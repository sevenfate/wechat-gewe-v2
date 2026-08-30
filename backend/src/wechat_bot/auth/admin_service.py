from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.auth.admin_schemas import (
    AdminUserCreate,
    AdminUserView,
    RbacPermissionView,
    RbacRoleCreate,
    RbacRoleView,
)
from wechat_bot.auth.constants import OWNER_ROLE_CODE
from wechat_bot.auth.passwords import password_manager
from wechat_bot.auth.service import AuthPrincipal, normalize_username
from wechat_bot.db.auth_models import (
    AdminSession,
    AdminUser,
    AdminUserStatus,
    AuthEventOutcome,
    AuthSecurityEvent,
    RbacPermission,
    RbacRole,
    RbacRolePermission,
    RbacUserRole,
)
from wechat_bot.db.base import utc_now


class AdminRbacError(Exception):
    """Base class for expected RBAC management errors."""


class AdminUserNotFoundError(AdminRbacError):
    pass


class AdminRoleNotFoundError(AdminRbacError):
    pass


class DuplicateAdminIdentityError(AdminRbacError):
    pass


class InvalidRbacBindingError(AdminRbacError):
    pass


class LastActiveOwnerError(AdminRbacError):
    pass


class OwnerPrivilegesRequiredError(AdminRbacError):
    pass


class SystemRoleProtectedError(AdminRbacError):
    pass


class AdminRbacService:
    async def list_users(self, session: AsyncSession) -> list[AdminUserView]:
        users = list(
            await session.scalars(
                select(AdminUser).order_by(AdminUser.username_normalized, AdminUser.id)
            )
        )
        role_codes = await self._user_role_codes(session, [user.id for user in users])
        return [self._user_view(user, role_codes.get(user.id, ())) for user in users]

    async def create_user(
        self,
        session: AsyncSession,
        payload: AdminUserCreate,
        actor: AuthPrincipal,
    ) -> AdminUserView:
        canonical_username, normalized_username = normalize_username(payload.username)
        existing_id = await session.scalar(
            select(AdminUser.id).where(AdminUser.username_normalized == normalized_username)
        )
        if existing_id is not None:
            raise DuplicateAdminIdentityError("administrator username already exists")

        user = AdminUser(
            username=canonical_username,
            username_normalized=normalized_username,
            display_name=payload.display_name,
            password_hash=password_manager.hash(payload.password.get_secret_value()),
            status=AdminUserStatus.ACTIVE,
            auth_version=1,
        )
        try:
            async with session.begin_nested():
                session.add(user)
                await session.flush()
        except IntegrityError as exc:
            raise DuplicateAdminIdentityError("administrator username already exists") from exc

        self._record_change(
            session,
            actor,
            event_type="auth.admin.user.created",
            target_type="admin_user",
            target_id=user.id,
            detail={"username": user.username},
        )
        return self._user_view(user, ())

    async def set_user_status(
        self,
        session: AsyncSession,
        user_id: UUID,
        status: AdminUserStatus,
        actor: AuthPrincipal,
    ) -> AdminUserView:
        user = await self._load_user_for_update(session, user_id)
        role_codes = await self._role_codes_for_user(session, user.id)
        if OWNER_ROLE_CODE in role_codes and not actor.is_owner:
            raise OwnerPrivilegesRequiredError("only an Owner can modify an Owner account")
        if user.status == status:
            return self._user_view(user, role_codes)
        if status == AdminUserStatus.DISABLED and OWNER_ROLE_CODE in role_codes:
            await self._assert_another_active_owner_exists(session, excluding_user_id=user.id)

        previous_status = user.status
        user.status = status
        await self._invalidate_users(session, [user], reason="admin_user_status_changed")
        self._record_change(
            session,
            actor,
            event_type="auth.admin.user.status_changed",
            target_type="admin_user",
            target_id=user.id,
            detail={"from": previous_status.value, "to": status.value},
        )
        await session.flush()
        return self._user_view(user, role_codes)

    async def replace_user_roles(
        self,
        session: AsyncSession,
        user_id: UUID,
        role_codes: Sequence[str],
        actor: AuthPrincipal,
    ) -> AdminUserView:
        self._require_owner(actor)
        user = await self._load_user_for_update(session, user_id)
        roles = await self._resolve_roles(session, role_codes)
        requested_codes = {role.code for role in roles}
        current_rows = list(
            await session.scalars(
                select(RbacUserRole).where(RbacUserRole.user_id == user.id).with_for_update()
            )
        )
        current_role_ids = {row.role_id for row in current_rows}
        current_codes = await self._role_codes_for_user(session, user.id)
        if current_codes == requested_codes:
            return self._user_view(user, current_codes)
        if (
            user.status == AdminUserStatus.ACTIVE
            and OWNER_ROLE_CODE in current_codes
            and OWNER_ROLE_CODE not in requested_codes
        ):
            await self._assert_another_active_owner_exists(session, excluding_user_id=user.id)

        requested_role_ids = {role.id for role in roles}
        removed_ids = current_role_ids - requested_role_ids
        if removed_ids:
            await session.execute(
                delete(RbacUserRole).where(
                    RbacUserRole.user_id == user.id,
                    RbacUserRole.role_id.in_(removed_ids),
                )
            )
        session.add_all(
            RbacUserRole(user_id=user.id, role_id=role_id)
            for role_id in requested_role_ids - current_role_ids
        )
        await self._invalidate_users(session, [user], reason="admin_user_roles_changed")
        self._record_change(
            session,
            actor,
            event_type="auth.admin.user.roles_changed",
            target_type="admin_user",
            target_id=user.id,
            detail={"from": sorted(current_codes), "to": sorted(requested_codes)},
        )
        await session.flush()
        return self._user_view(user, requested_codes)

    async def list_roles(self, session: AsyncSession) -> list[RbacRoleView]:
        roles = list(await session.scalars(select(RbacRole).order_by(RbacRole.code)))
        permissions = await self._role_permission_codes(session, [role.id for role in roles])
        return [self._role_view(role, permissions.get(role.id, ())) for role in roles]

    async def create_role(
        self,
        session: AsyncSession,
        payload: RbacRoleCreate,
        actor: AuthPrincipal,
    ) -> RbacRoleView:
        self._require_owner(actor)
        if payload.code == OWNER_ROLE_CODE:
            raise SystemRoleProtectedError("the Owner system role cannot be created or replaced")
        existing_id = await session.scalar(select(RbacRole.id).where(RbacRole.code == payload.code))
        if existing_id is not None:
            raise DuplicateAdminIdentityError("role code already exists")

        role = RbacRole(
            code=payload.code,
            name=payload.name,
            is_system=False,
            active=True,
        )
        try:
            async with session.begin_nested():
                session.add(role)
                await session.flush()
        except IntegrityError as exc:
            raise DuplicateAdminIdentityError("role code already exists") from exc

        self._record_change(
            session,
            actor,
            event_type="auth.admin.role.created",
            target_type="rbac_role",
            target_id=role.id,
            detail={"code": role.code},
        )
        return self._role_view(role, ())

    async def replace_role_permissions(
        self,
        session: AsyncSession,
        role_id: UUID,
        permission_codes: Sequence[str],
        actor: AuthPrincipal,
    ) -> RbacRoleView:
        self._require_owner(actor)
        role = await session.scalar(
            select(RbacRole).where(RbacRole.id == role_id).with_for_update()
        )
        if role is None:
            raise AdminRoleNotFoundError("role not found")
        if role.code == OWNER_ROLE_CODE:
            raise SystemRoleProtectedError("the Owner system role has implicit full access")

        permissions = await self._resolve_permissions(session, permission_codes)
        requested_codes = {permission.code for permission in permissions}
        current_rows = list(
            await session.scalars(
                select(RbacRolePermission)
                .where(RbacRolePermission.role_id == role.id)
                .with_for_update()
            )
        )
        current_permission_ids = {row.permission_id for row in current_rows}
        current_codes = await self._permission_codes_for_role(session, role.id)
        if current_codes == requested_codes:
            return self._role_view(role, current_codes)

        requested_permission_ids = {permission.id for permission in permissions}
        removed_ids = current_permission_ids - requested_permission_ids
        if removed_ids:
            await session.execute(
                delete(RbacRolePermission).where(
                    RbacRolePermission.role_id == role.id,
                    RbacRolePermission.permission_id.in_(removed_ids),
                )
            )
        session.add_all(
            RbacRolePermission(role_id=role.id, permission_id=permission_id)
            for permission_id in requested_permission_ids - current_permission_ids
        )

        affected_users = list(
            await session.scalars(
                select(AdminUser)
                .join(RbacUserRole, RbacUserRole.user_id == AdminUser.id)
                .where(RbacUserRole.role_id == role.id)
                .with_for_update()
            )
        )
        await self._invalidate_users(
            session,
            affected_users,
            reason="admin_role_permissions_changed",
        )
        self._record_change(
            session,
            actor,
            event_type="auth.admin.role.permissions_changed",
            target_type="rbac_role",
            target_id=role.id,
            detail={"from": sorted(current_codes), "to": sorted(requested_codes)},
        )
        await session.flush()
        return self._role_view(role, requested_codes)

    async def list_permissions(self, session: AsyncSession) -> list[RbacPermissionView]:
        permissions = list(
            await session.scalars(select(RbacPermission).order_by(RbacPermission.code))
        )
        return [
            RbacPermissionView(
                id=permission.id,
                code=permission.code,
                description=permission.description,
            )
            for permission in permissions
        ]

    @staticmethod
    async def _load_user_for_update(session: AsyncSession, user_id: UUID) -> AdminUser:
        user = await session.scalar(
            select(AdminUser).where(AdminUser.id == user_id).with_for_update()
        )
        if user is None:
            raise AdminUserNotFoundError("administrator user not found")
        return user

    @staticmethod
    async def _resolve_roles(
        session: AsyncSession,
        role_codes: Sequence[str],
    ) -> list[RbacRole]:
        if not role_codes:
            return []
        roles = list(
            await session.scalars(
                select(RbacRole).where(RbacRole.code.in_(role_codes)).with_for_update()
            )
        )
        found_codes = {role.code for role in roles}
        missing_codes = set(role_codes) - found_codes
        if missing_codes:
            raise InvalidRbacBindingError(f"unknown role codes: {', '.join(sorted(missing_codes))}")
        inactive_codes = {role.code for role in roles if not role.active}
        if inactive_codes:
            raise InvalidRbacBindingError(
                f"inactive role codes: {', '.join(sorted(inactive_codes))}"
            )
        return roles

    @staticmethod
    async def _resolve_permissions(
        session: AsyncSession,
        permission_codes: Sequence[str],
    ) -> list[RbacPermission]:
        if not permission_codes:
            return []
        permissions = list(
            await session.scalars(
                select(RbacPermission)
                .where(RbacPermission.code.in_(permission_codes))
                .with_for_update()
            )
        )
        found_codes = {permission.code for permission in permissions}
        missing_codes = set(permission_codes) - found_codes
        if missing_codes:
            raise InvalidRbacBindingError(
                f"unknown permission codes: {', '.join(sorted(missing_codes))}"
            )
        return permissions

    @staticmethod
    async def _role_codes_for_user(session: AsyncSession, user_id: UUID) -> set[str]:
        return set(
            await session.scalars(
                select(RbacRole.code)
                .join(RbacUserRole, RbacUserRole.role_id == RbacRole.id)
                .where(RbacUserRole.user_id == user_id)
            )
        )

    @staticmethod
    async def _permission_codes_for_role(session: AsyncSession, role_id: UUID) -> set[str]:
        return set(
            await session.scalars(
                select(RbacPermission.code)
                .join(
                    RbacRolePermission,
                    RbacRolePermission.permission_id == RbacPermission.id,
                )
                .where(RbacRolePermission.role_id == role_id)
            )
        )

    @staticmethod
    async def _user_role_codes(
        session: AsyncSession,
        user_ids: Sequence[UUID],
    ) -> dict[UUID, list[str]]:
        if not user_ids:
            return {}
        rows = (
            await session.execute(
                select(RbacUserRole.user_id, RbacRole.code)
                .join(RbacRole, RbacRole.id == RbacUserRole.role_id)
                .where(RbacUserRole.user_id.in_(user_ids))
                .order_by(RbacRole.code)
            )
        ).all()
        result: dict[UUID, list[str]] = {}
        for user_id, code in rows:
            result.setdefault(user_id, []).append(code)
        return result

    @staticmethod
    async def _role_permission_codes(
        session: AsyncSession,
        role_ids: Sequence[UUID],
    ) -> dict[UUID, list[str]]:
        if not role_ids:
            return {}
        rows = (
            await session.execute(
                select(RbacRolePermission.role_id, RbacPermission.code)
                .join(
                    RbacPermission,
                    RbacPermission.id == RbacRolePermission.permission_id,
                )
                .where(RbacRolePermission.role_id.in_(role_ids))
                .order_by(RbacPermission.code)
            )
        ).all()
        result: dict[UUID, list[str]] = {}
        for role_id, code in rows:
            result.setdefault(role_id, []).append(code)
        return result

    @staticmethod
    async def _assert_another_active_owner_exists(
        session: AsyncSession,
        *,
        excluding_user_id: UUID,
    ) -> None:
        owner_role = await session.scalar(
            select(RbacRole).where(RbacRole.code == OWNER_ROLE_CODE).with_for_update()
        )
        if owner_role is None:
            raise LastActiveOwnerError("at least one active Owner must remain")
        remaining_count = await session.scalar(
            select(func.count(AdminUser.id))
            .select_from(AdminUser)
            .join(RbacUserRole, RbacUserRole.user_id == AdminUser.id)
            .where(
                RbacUserRole.role_id == owner_role.id,
                AdminUser.status == AdminUserStatus.ACTIVE,
                AdminUser.id != excluding_user_id,
            )
        )
        if not remaining_count:
            raise LastActiveOwnerError("at least one active Owner must remain")

    @staticmethod
    async def _invalidate_users(
        session: AsyncSession,
        users: Sequence[AdminUser],
        *,
        reason: str,
    ) -> None:
        if not users:
            return
        user_ids = [user.id for user in users]
        for user in users:
            user.auth_version += 1
        await session.execute(
            update(AdminSession)
            .where(AdminSession.user_id.in_(user_ids), AdminSession.revoked_at.is_(None))
            .values(revoked_at=utc_now(), revoked_reason=reason)
        )

    @staticmethod
    def _require_owner(actor: AuthPrincipal) -> None:
        if not actor.is_owner:
            raise OwnerPrivilegesRequiredError("Owner privileges are required")

    @staticmethod
    def _record_change(
        session: AsyncSession,
        actor: AuthPrincipal,
        *,
        event_type: str,
        target_type: str,
        target_id: UUID,
        detail: dict[str, object],
    ) -> None:
        session.add(
            AuthSecurityEvent(
                event_type=event_type,
                outcome=AuthEventOutcome.SUCCESS,
                user_id=actor.user_id,
                session_id=actor.session_id,
                username_normalized=actor.username.casefold(),
                source_key_hash=None,
                detail={
                    "target_type": target_type,
                    "target_id": str(target_id),
                    **detail,
                },
            )
        )

    @staticmethod
    def _user_view(user: AdminUser, role_codes: Sequence[str] | set[str]) -> AdminUserView:
        return AdminUserView(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            status=user.status,
            auth_version=user.auth_version,
            roles=sorted(role_codes),
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

    @staticmethod
    def _role_view(
        role: RbacRole,
        permission_codes: Sequence[str] | set[str],
    ) -> RbacRoleView:
        return RbacRoleView(
            id=role.id,
            code=role.code,
            name=role.name,
            is_system=role.is_system,
            active=role.active,
            permissions=sorted(permission_codes),
            created_at=role.created_at,
            updated_at=role.updated_at,
        )
