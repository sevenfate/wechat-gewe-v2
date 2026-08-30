from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from wechat_bot.auth.constants import (
    BOOTSTRAP_HEADER_NAME,
    COOKIE_PATH,
    COOKIE_SAME_SITE,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    PRE_AUTH_CSRF_TTL_SECONDS,
    SESSION_COOKIE_NAME,
)
from wechat_bot.auth.dependencies import (
    AuthenticatedCsrfDependency,
    CurrentPrincipalDependency,
    DatabaseSessionDependency,
)
from wechat_bot.auth.schemas import (
    AuthUserResponse,
    BootstrapOwnerRequest,
    CsrfResponse,
    LoginRequest,
    LoginResponse,
    MessageResponse,
)
from wechat_bot.auth.service import (
    AuthPrincipal,
    BootstrapAlreadyCompleted,
    BootstrapNotConfigured,
    InvalidBootstrapToken,
    InvalidCredentials,
    IssuedSession,
    LoginRateLimited,
    bootstrap_owner,
    login,
    revoke_session,
)
from wechat_bot.auth.tokens import generate_token, secrets_equal
from wechat_bot.core.config import Settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _source_identifier(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _cookies_are_secure(settings: Settings) -> bool:
    return not settings.is_local


def set_pre_auth_csrf_cookie(response: Response, csrf_token: str, settings: Settings) -> None:
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=PRE_AUTH_CSRF_TTL_SECONDS,
        secure=_cookies_are_secure(settings),
        httponly=False,
        samesite=COOKIE_SAME_SITE,
        path=COOKIE_PATH,
    )


def set_session_cookies(
    response: Response,
    issued_session: IssuedSession,
    settings: Settings,
) -> None:
    secure = _cookies_are_secure(settings)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=issued_session.token,
        max_age=settings.auth_session_absolute_seconds,
        secure=secure,
        httponly=True,
        samesite=COOKIE_SAME_SITE,
        path=COOKIE_PATH,
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=issued_session.csrf_token,
        max_age=settings.auth_session_absolute_seconds,
        secure=secure,
        httponly=False,
        samesite=COOKIE_SAME_SITE,
        path=COOKIE_PATH,
    )


def clear_auth_cookies(response: Response, settings: Settings) -> None:
    secure = _cookies_are_secure(settings)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path=COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=COOKIE_SAME_SITE,
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path=COOKIE_PATH,
        secure=secure,
        httponly=False,
        samesite=COOKIE_SAME_SITE,
    )


def _user_response(principal: AuthPrincipal) -> AuthUserResponse:
    return AuthUserResponse(
        id=principal.user_id,
        username=principal.username,
        display_name=principal.display_name,
        roles=sorted(principal.roles),
        permissions=sorted(principal.permissions),
    )


def _validate_pre_auth_csrf(request: Request) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if not cookie_token or not header_token or not secrets_equal(cookie_token, header_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        )


@router.get("/csrf", response_model=CsrfResponse)
async def issue_pre_auth_csrf(request: Request, response: Response) -> CsrfResponse:
    csrf_token = generate_token()
    settings = cast(Settings, request.app.state.settings)
    set_pre_auth_csrf_cookie(response, csrf_token, settings)
    return CsrfResponse(csrf_token=csrf_token)


@router.post(
    "/bootstrap",
    response_model=AuthUserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_initial_owner(
    payload: BootstrapOwnerRequest,
    request: Request,
    database: DatabaseSessionDependency,
    bootstrap_token: Annotated[str, Header(alias=BOOTSTRAP_HEADER_NAME)],
) -> AuthUserResponse:
    settings = cast(Settings, request.app.state.settings)
    try:
        owner = await bootstrap_owner(
            database,
            settings,
            presented_token=bootstrap_token,
            username=payload.username,
            password=payload.password.get_secret_value(),
            display_name=payload.display_name,
            source_identifier=_source_identifier(request),
        )
    except BootstrapNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="bootstrap is not configured",
        ) from exc
    except InvalidBootstrapToken as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="bootstrap token is invalid",
        ) from exc
    except BootstrapAlreadyCompleted as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="bootstrap has already been completed",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid bootstrap account data",
        ) from exc

    return AuthUserResponse(
        id=owner.id,
        username=owner.username,
        display_name=owner.display_name,
        roles=["owner"],
        permissions=[],
    )


@router.post("/login", response_model=LoginResponse)
async def create_session(
    payload: LoginRequest,
    request: Request,
    response: Response,
    database: DatabaseSessionDependency,
) -> LoginResponse:
    _validate_pre_auth_csrf(request)
    settings = cast(Settings, request.app.state.settings)
    try:
        result = await login(
            database,
            settings,
            username=payload.username,
            password=payload.password.get_secret_value(),
            source_identifier=_source_identifier(request),
            user_agent=request.headers.get("User-Agent", ""),
        )
    except LoginRateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login attempts",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except (InvalidCredentials, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
        ) from exc

    set_session_cookies(response, result.issued_session, settings)
    return LoginResponse(
        user=_user_response(result.principal),
        csrf_token=result.issued_session.csrf_token,
        idle_expires_at=result.issued_session.idle_expires_at,
        absolute_expires_at=result.issued_session.absolute_expires_at,
    )


@router.get("/me", response_model=AuthUserResponse)
async def read_current_user(principal: CurrentPrincipalDependency) -> AuthUserResponse:
    return _user_response(principal)


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    response: Response,
    database: DatabaseSessionDependency,
    context: AuthenticatedCsrfDependency,
) -> MessageResponse:
    await revoke_session(database, context)
    clear_auth_cookies(response, cast(Settings, request.app.state.settings))
    return MessageResponse(message="logged out")
