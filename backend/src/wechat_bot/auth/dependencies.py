from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.api.dependencies import get_session
from wechat_bot.auth.constants import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from wechat_bot.auth.service import (
    AuthContext,
    AuthPrincipal,
    CsrfRejected,
    InvalidSession,
    authenticate_session,
    validate_csrf,
)
from wechat_bot.core.config import Settings

_PERMISSION_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*$")

DatabaseSessionDependency = Annotated[AsyncSession, Depends(get_session)]


async def get_auth_context(
    request: Request,
    database: DatabaseSessionDependency,
) -> AuthContext:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    settings = cast(Settings, request.app.state.settings)
    try:
        return await authenticate_session(database, settings, token)
    except InvalidSession as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Session"},
        ) from exc


AuthContextDependency = Annotated[AuthContext, Depends(get_auth_context)]


async def get_current_principal(context: AuthContextDependency) -> AuthPrincipal:
    return context.principal


CurrentPrincipalDependency = Annotated[AuthPrincipal, Depends(get_current_principal)]


async def require_authenticated_csrf(
    request: Request,
    context: AuthContextDependency,
) -> AuthContext:
    try:
        validate_csrf(
            context,
            request.cookies.get(CSRF_COOKIE_NAME),
            request.headers.get(CSRF_HEADER_NAME),
        )
    except CsrfRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        ) from exc
    return context


AuthenticatedCsrfDependency = Annotated[AuthContext, Depends(require_authenticated_csrf)]


async def require_management_request(
    request: Request,
    context: AuthContextDependency,
) -> AuthContext:
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        try:
            validate_csrf(
                context,
                request.cookies.get(CSRF_COOKIE_NAME),
                request.headers.get(CSRF_HEADER_NAME),
            )
        except CsrfRejected as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed",
            ) from exc
    return context


ManagementRequestDependency = Annotated[AuthContext, Depends(require_management_request)]


async def require_owner(principal: CurrentPrincipalDependency) -> AuthPrincipal:
    if not principal.is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission denied")
    return principal


def require_permission(
    permission_code: str,
) -> Callable[[AuthPrincipal], Awaitable[AuthPrincipal]]:
    if _PERMISSION_CODE_PATTERN.fullmatch(permission_code) is None:
        raise ValueError("permission code is not valid")

    async def permission_dependency(
        principal: CurrentPrincipalDependency,
    ) -> AuthPrincipal:
        if not principal.is_owner and permission_code not in principal.permissions:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission denied")
        return principal

    return permission_dependency
