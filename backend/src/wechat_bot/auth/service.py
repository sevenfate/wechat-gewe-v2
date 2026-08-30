from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.auth.constants import OWNER_ROLE_CODE, SYSTEM_PERMISSION_CATALOG
from wechat_bot.auth.passwords import password_manager
from wechat_bot.auth.tokens import (
    fingerprint_token,
    generate_token,
    hash_context,
    hash_token,
    secrets_equal,
)
from wechat_bot.core.config import Settings
from wechat_bot.db.auth_models import (
    AdminSession,
    AdminUser,
    AdminUserStatus,
    AuthBootstrapState,
    AuthEventOutcome,
    AuthLoginThrottle,
    AuthSecurityEvent,
    LoginThrottleDimension,
    RbacPermission,
    RbacRole,
    RbacRolePermission,
    RbacUserRole,
)
from wechat_bot.db.base import utc_now


class AuthServiceError(Exception):
    """Base class for authentication failures safe to translate at the API boundary."""


class BootstrapNotConfigured(AuthServiceError):
    pass


class BootstrapAlreadyCompleted(AuthServiceError):
    pass


class InvalidBootstrapToken(AuthServiceError):
    pass


class InvalidCredentials(AuthServiceError):
    pass


class LoginRateLimited(AuthServiceError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("login rate limit exceeded")
        self.retry_after_seconds = retry_after_seconds


class InvalidSession(AuthServiceError):
    pass


class CsrfRejected(AuthServiceError):
    pass


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session_id: UUID
    token: str
    csrf_token: str
    idle_expires_at: datetime
    absolute_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    user_id: UUID
    session_id: UUID
    username: str
    display_name: str | None
    roles: frozenset[str]
    permissions: frozenset[str]

    @property
    def is_owner(self) -> bool:
        return OWNER_ROLE_CODE in self.roles


@dataclass(frozen=True, slots=True)
class AuthContext:
    principal: AuthPrincipal
    csrf_token_hash: str


@dataclass(frozen=True, slots=True)
class LoginResult:
    principal: AuthPrincipal
    issued_session: IssuedSession


def normalize_username(username: str) -> tuple[str, str]:
    canonical = unicodedata.normalize("NFKC", username).strip()
    if not 3 <= len(canonical) <= 80:
        raise ValueError("username must contain between 3 and 80 characters")
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in canonical
    ):
        raise ValueError("username cannot contain whitespace or control characters")
    return canonical, canonical.casefold()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _record_security_event(
    database: AsyncSession,
    *,
    event_type: str,
    outcome: AuthEventOutcome,
    source_key_hash: str | None,
    user_id: UUID | None = None,
    session_id: UUID | None = None,
    username_normalized: str | None = None,
    detail: dict[str, str | int | bool] | None = None,
) -> None:
    database.add(
        AuthSecurityEvent(
            event_type=event_type,
            outcome=outcome,
            user_id=user_id,
            session_id=session_id,
            username_normalized=username_normalized,
            source_key_hash=source_key_hash,
            detail=detail or {},
        )
    )


async def _owner_exists(database: AsyncSession) -> bool:
    owner_count = await database.scalar(
        select(func.count(AdminUser.id))
        .select_from(AdminUser)
        .join(RbacUserRole, RbacUserRole.user_id == AdminUser.id)
        .join(RbacRole, RbacRole.id == RbacUserRole.role_id)
        .where(RbacRole.code == OWNER_ROLE_CODE)
    )
    return bool(owner_count)


async def ensure_system_permissions(database: AsyncSession) -> None:
    existing_codes = set(
        await database.scalars(
            select(RbacPermission.code).where(RbacPermission.code.in_(SYSTEM_PERMISSION_CATALOG))
        )
    )
    database.add_all(
        RbacPermission(code=code, description=description)
        for code, description in SYSTEM_PERMISSION_CATALOG.items()
        if code not in existing_codes
    )


async def bootstrap_owner(
    database: AsyncSession,
    settings: Settings,
    *,
    presented_token: str,
    username: str,
    password: str,
    display_name: str | None,
    source_identifier: str,
) -> AdminUser:
    canonical_username, normalized_username = normalize_username(username)
    source_key_hash = hash_context(f"source:{source_identifier}")
    configured_token = settings.auth_bootstrap_token

    state_exists = await database.scalar(select(AuthBootstrapState.id).limit(1))
    if state_exists is not None or await _owner_exists(database):
        _record_security_event(
            database,
            event_type="auth.bootstrap",
            outcome=AuthEventOutcome.DENIED,
            source_key_hash=source_key_hash,
            username_normalized=normalized_username,
            detail={"reason": "already_completed"},
        )
        await database.commit()
        raise BootstrapAlreadyCompleted

    if configured_token is None:
        _record_security_event(
            database,
            event_type="auth.bootstrap",
            outcome=AuthEventOutcome.DENIED,
            source_key_hash=source_key_hash,
            username_normalized=normalized_username,
            detail={"reason": "not_configured"},
        )
        await database.commit()
        raise BootstrapNotConfigured

    configured_token_value = configured_token.get_secret_value()
    if not secrets_equal(configured_token_value, presented_token):
        _record_security_event(
            database,
            event_type="auth.bootstrap",
            outcome=AuthEventOutcome.DENIED,
            source_key_hash=source_key_hash,
            username_normalized=normalized_username,
            detail={"reason": "invalid_token"},
        )
        await database.commit()
        raise InvalidBootstrapToken

    password_hash = password_manager.hash(password)
    try:
        owner_role = await database.scalar(
            select(RbacRole).where(RbacRole.code == OWNER_ROLE_CODE).with_for_update()
        )
        if owner_role is None:
            owner_role = RbacRole(
                code=OWNER_ROLE_CODE,
                name="Owner",
                is_system=True,
                active=True,
            )
            database.add(owner_role)
            await database.flush()

        await ensure_system_permissions(database)

        owner = AdminUser(
            username=canonical_username,
            username_normalized=normalized_username,
            display_name=display_name,
            password_hash=password_hash,
            status=AdminUserStatus.ACTIVE,
            auth_version=1,
        )
        database.add(owner)
        await database.flush()
        database.add(RbacUserRole(user_id=owner.id, role_id=owner_role.id))
        database.add(
            AuthBootstrapState(
                id=1,
                owner_user_id=owner.id,
                token_fingerprint=fingerprint_token(configured_token_value),
            )
        )
        _record_security_event(
            database,
            event_type="auth.bootstrap",
            outcome=AuthEventOutcome.SUCCESS,
            source_key_hash=source_key_hash,
            user_id=owner.id,
            username_normalized=normalized_username,
        )
        await database.commit()
    except IntegrityError as exc:
        await database.rollback()
        _record_security_event(
            database,
            event_type="auth.bootstrap",
            outcome=AuthEventOutcome.DENIED,
            source_key_hash=source_key_hash,
            username_normalized=normalized_username,
            detail={"reason": "concurrent_or_conflicting_bootstrap"},
        )
        await database.commit()
        raise BootstrapAlreadyCompleted from exc

    return owner


async def _load_throttle(
    database: AsyncSession,
    dimension: LoginThrottleDimension,
    key_hash: str,
) -> AuthLoginThrottle | None:
    result = await database.execute(
        select(AuthLoginThrottle)
        .where(
            AuthLoginThrottle.dimension == dimension,
            AuthLoginThrottle.key_hash == key_hash,
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


def _active_block_seconds(record: AuthLoginThrottle | None, now: datetime) -> int:
    if record is None or record.blocked_until is None:
        return 0
    remaining = (_as_utc(record.blocked_until) - now).total_seconds()
    return max(0, math.ceil(remaining))


async def _record_login_failure(
    database: AsyncSession,
    settings: Settings,
    *,
    dimension: LoginThrottleDimension,
    key_hash: str,
    now: datetime,
) -> None:
    record = await _load_throttle(database, dimension, key_hash)
    window = timedelta(seconds=settings.auth_login_window_seconds)
    if record is None:
        record = AuthLoginThrottle(
            dimension=dimension,
            key_hash=key_hash,
            window_started_at=now,
            failure_count=0,
        )
        database.add(record)
    elif now >= _as_utc(record.window_started_at) + window:
        record.window_started_at = now
        record.failure_count = 0
        record.blocked_until = None

    record.failure_count += 1
    if record.failure_count >= settings.auth_login_max_failures:
        record.blocked_until = now + timedelta(seconds=settings.auth_login_block_seconds)


async def _clear_account_throttle(database: AsyncSession, account_key_hash: str) -> None:
    await database.execute(
        delete(AuthLoginThrottle).where(
            AuthLoginThrottle.dimension == LoginThrottleDimension.ACCOUNT,
            AuthLoginThrottle.key_hash == account_key_hash,
        )
    )


async def _create_session(
    database: AsyncSession,
    settings: Settings,
    user: AdminUser,
    *,
    source_key_hash: str,
    user_agent: str,
    now: datetime,
) -> IssuedSession:
    token = generate_token()
    csrf_token = generate_token()
    absolute_expires_at = now + timedelta(seconds=settings.auth_session_absolute_seconds)
    idle_expires_at = min(
        now + timedelta(seconds=settings.auth_session_idle_seconds), absolute_expires_at
    )
    session_record = AdminSession(
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token_hash=hash_token(csrf_token),
        auth_version=user.auth_version,
        last_seen_at=now,
        idle_expires_at=idle_expires_at,
        absolute_expires_at=absolute_expires_at,
        source_key_hash=source_key_hash,
        user_agent_hash=hash_context(user_agent) if user_agent else None,
    )
    database.add(session_record)
    await database.flush()
    return IssuedSession(
        session_id=session_record.id,
        token=token,
        csrf_token=csrf_token,
        idle_expires_at=idle_expires_at,
        absolute_expires_at=absolute_expires_at,
    )


async def _load_principal(
    database: AsyncSession,
    user: AdminUser,
    session_id: UUID,
) -> AuthPrincipal:
    rows = (
        await database.execute(
            select(RbacRole.code, RbacPermission.code)
            .join(RbacUserRole, RbacUserRole.role_id == RbacRole.id)
            .outerjoin(RbacRolePermission, RbacRolePermission.role_id == RbacRole.id)
            .outerjoin(RbacPermission, RbacPermission.id == RbacRolePermission.permission_id)
            .where(RbacUserRole.user_id == user.id, RbacRole.active.is_(True))
        )
    ).all()
    roles = frozenset(role_code for role_code, _ in rows)
    permissions = frozenset(permission_code for _, permission_code in rows if permission_code)
    return AuthPrincipal(
        user_id=user.id,
        session_id=session_id,
        username=user.username,
        display_name=user.display_name,
        roles=roles,
        permissions=permissions,
    )


async def login(
    database: AsyncSession,
    settings: Settings,
    *,
    username: str,
    password: str,
    source_identifier: str,
    user_agent: str,
) -> LoginResult:
    try:
        _, normalized_username = normalize_username(username)
        username_is_valid = True
    except ValueError:
        normalized_username = unicodedata.normalize("NFKC", username).strip().casefold()
        username_is_valid = False
    audited_username = normalized_username if username_is_valid else None
    now = utc_now()
    account_key_hash = hash_context(f"account:{normalized_username}")
    source_key_hash = hash_context(f"source:{source_identifier}")
    account_throttle = await _load_throttle(
        database, LoginThrottleDimension.ACCOUNT, account_key_hash
    )
    source_throttle = await _load_throttle(database, LoginThrottleDimension.SOURCE, source_key_hash)
    retry_after = max(
        _active_block_seconds(account_throttle, now),
        _active_block_seconds(source_throttle, now),
    )
    if retry_after:
        _record_security_event(
            database,
            event_type="auth.login",
            outcome=AuthEventOutcome.RATE_LIMITED,
            source_key_hash=source_key_hash,
            username_normalized=audited_username,
            detail={"retry_after_seconds": retry_after},
        )
        await database.commit()
        raise LoginRateLimited(retry_after)

    user = None
    if username_is_valid:
        user = await database.scalar(
            select(AdminUser).where(AdminUser.username_normalized == normalized_username)
        )
    if user is None:
        password_manager.verify_dummy(password)
        password_valid = False
    else:
        password_valid = password_manager.verify(user.password_hash, password)

    if user is None or not password_valid or user.status != AdminUserStatus.ACTIVE:
        await _record_login_failure(
            database,
            settings,
            dimension=LoginThrottleDimension.ACCOUNT,
            key_hash=account_key_hash,
            now=now,
        )
        await _record_login_failure(
            database,
            settings,
            dimension=LoginThrottleDimension.SOURCE,
            key_hash=source_key_hash,
            now=now,
        )
        _record_security_event(
            database,
            event_type="auth.login",
            outcome=AuthEventOutcome.DENIED,
            source_key_hash=source_key_hash,
            user_id=user.id if user is not None else None,
            username_normalized=audited_username,
            detail={"reason": "invalid_credentials"},
        )
        await database.commit()
        raise InvalidCredentials

    if password_manager.needs_rehash(user.password_hash):
        user.password_hash = password_manager.hash(password)
    user.last_login_at = now
    await _clear_account_throttle(database, account_key_hash)
    issued_session = await _create_session(
        database,
        settings,
        user,
        source_key_hash=source_key_hash,
        user_agent=user_agent,
        now=now,
    )
    principal = await _load_principal(database, user, issued_session.session_id)
    _record_security_event(
        database,
        event_type="auth.login",
        outcome=AuthEventOutcome.SUCCESS,
        source_key_hash=source_key_hash,
        user_id=user.id,
        session_id=issued_session.session_id,
        username_normalized=normalized_username,
    )
    await database.commit()
    return LoginResult(principal=principal, issued_session=issued_session)


async def authenticate_session(
    database: AsyncSession,
    settings: Settings,
    token: str,
) -> AuthContext:
    if not token:
        raise InvalidSession
    row = (
        await database.execute(
            select(AdminSession, AdminUser)
            .join(AdminUser, AdminUser.id == AdminSession.user_id)
            .where(AdminSession.token_hash == hash_token(token))
        )
    ).one_or_none()
    if row is None:
        raise InvalidSession

    session_record, user = row
    now = utc_now()
    invalid_reason: str | None = None
    if session_record.revoked_at is not None:
        invalid_reason = "revoked"
    elif user.status != AdminUserStatus.ACTIVE:
        invalid_reason = "user_disabled"
    elif session_record.auth_version != user.auth_version:
        invalid_reason = "auth_version_changed"
    elif now >= _as_utc(session_record.absolute_expires_at):
        invalid_reason = "absolute_expired"
    elif now >= _as_utc(session_record.idle_expires_at):
        invalid_reason = "idle_expired"

    if invalid_reason is not None:
        if session_record.revoked_at is None:
            session_record.revoked_at = now
            session_record.revoked_reason = invalid_reason
            _record_security_event(
                database,
                event_type="auth.session.invalidated",
                outcome=AuthEventOutcome.REVOKED,
                source_key_hash=session_record.source_key_hash,
                user_id=user.id,
                session_id=session_record.id,
                username_normalized=user.username_normalized,
                detail={"reason": invalid_reason},
            )
            await database.commit()
        raise InvalidSession

    absolute_expires_at = _as_utc(session_record.absolute_expires_at)
    session_record.last_seen_at = now
    session_record.idle_expires_at = min(
        now + timedelta(seconds=settings.auth_session_idle_seconds), absolute_expires_at
    )
    principal = await _load_principal(database, user, session_record.id)
    csrf_token_hash = session_record.csrf_token_hash
    await database.commit()
    return AuthContext(principal=principal, csrf_token_hash=csrf_token_hash)


def validate_csrf(context: AuthContext, cookie_token: str | None, header_token: str | None) -> None:
    if not cookie_token or not header_token:
        raise CsrfRejected
    if not secrets_equal(cookie_token, header_token):
        raise CsrfRejected
    if not secrets_equal(hash_token(header_token), context.csrf_token_hash):
        raise CsrfRejected


async def revoke_session(
    database: AsyncSession,
    context: AuthContext,
    *,
    reason: str = "logout",
) -> None:
    session_record = await database.get(AdminSession, context.principal.session_id)
    if session_record is not None and session_record.revoked_at is None:
        session_record.revoked_at = utc_now()
        session_record.revoked_reason = reason
        _record_security_event(
            database,
            event_type="auth.logout",
            outcome=AuthEventOutcome.REVOKED,
            source_key_hash=session_record.source_key_hash,
            user_id=context.principal.user_id,
            session_id=context.principal.session_id,
            username_normalized=context.principal.username.casefold(),
        )
    await database.commit()


async def invalidate_user_sessions(
    database: AsyncSession,
    user_id: UUID,
    *,
    reason: str,
    disable_user: bool = False,
) -> None:
    user = await database.get(AdminUser, user_id)
    if user is None:
        raise ValueError("administrator user does not exist")
    user.auth_version += 1
    if disable_user:
        user.status = AdminUserStatus.DISABLED
    now = utc_now()
    await database.execute(
        update(AdminSession)
        .where(AdminSession.user_id == user_id, AdminSession.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    _record_security_event(
        database,
        event_type="auth.user.sessions_invalidated",
        outcome=AuthEventOutcome.REVOKED,
        source_key_hash=None,
        user_id=user.id,
        username_normalized=user.username_normalized,
        detail={"reason": reason, "disabled": disable_user},
    )
    await database.commit()
