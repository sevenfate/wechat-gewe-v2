from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from wechat_bot.auth.admin_schemas import (
    AdminUserCreate,
    AdminUserList,
    AdminUserStatusUpdate,
    AdminUserView,
    RbacPermissionList,
    RbacRoleCreate,
    RbacRoleList,
    RbacRoleView,
    RolePermissionBindingUpdate,
    UserRoleBindingUpdate,
)
from wechat_bot.auth.admin_service import (
    AdminRbacError,
    AdminRbacService,
    AdminRoleNotFoundError,
    AdminUserNotFoundError,
    DuplicateAdminIdentityError,
    InvalidRbacBindingError,
    LastActiveOwnerError,
    OwnerPrivilegesRequiredError,
    SystemRoleProtectedError,
)
from wechat_bot.auth.constants import ADMIN_USER_MANAGE_PERMISSION
from wechat_bot.auth.dependencies import (
    CurrentPrincipalDependency,
    DatabaseSessionDependency,
    require_management_request,
    require_owner,
    require_permission,
)

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["administrator RBAC"],
    dependencies=[
        Depends(require_management_request),
        Depends(require_permission(ADMIN_USER_MANAGE_PERMISSION)),
    ],
)
service = AdminRbacService()


@router.get("/users", response_model=AdminUserList)
async def list_admin_users(database: DatabaseSessionDependency) -> AdminUserList:
    items = await service.list_users(database)
    return AdminUserList(items=items, total=len(items))


@router.post(
    "/users",
    response_model=AdminUserView,
    status_code=status.HTTP_201_CREATED,
)
async def create_admin_user(
    payload: AdminUserCreate,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> AdminUserView:
    try:
        result = await service.create_user(database, payload, actor)
        await database.commit()
    except (DuplicateAdminIdentityError, ValueError) as exc:
        raise _http_error(exc) from exc
    return result


@router.patch("/users/{user_id}/status", response_model=AdminUserView)
async def change_admin_user_status(
    user_id: UUID,
    payload: AdminUserStatusUpdate,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> AdminUserView:
    try:
        result = await service.set_user_status(database, user_id, payload.status, actor)
        await database.commit()
    except AdminRbacError as exc:
        raise _http_error(exc) from exc
    return result


@router.put(
    "/users/{user_id}/roles",
    response_model=AdminUserView,
    dependencies=[Depends(require_owner)],
)
async def replace_admin_user_roles(
    user_id: UUID,
    payload: UserRoleBindingUpdate,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> AdminUserView:
    try:
        result = await service.replace_user_roles(database, user_id, payload.role_codes, actor)
        await database.commit()
    except AdminRbacError as exc:
        raise _http_error(exc) from exc
    return result


@router.get("/roles", response_model=RbacRoleList)
async def list_admin_roles(database: DatabaseSessionDependency) -> RbacRoleList:
    items = await service.list_roles(database)
    return RbacRoleList(items=items, total=len(items))


@router.post(
    "/roles",
    response_model=RbacRoleView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
)
async def create_admin_role(
    payload: RbacRoleCreate,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> RbacRoleView:
    try:
        result = await service.create_role(database, payload, actor)
        await database.commit()
    except AdminRbacError as exc:
        raise _http_error(exc) from exc
    return result


@router.put(
    "/roles/{role_id}/permissions",
    response_model=RbacRoleView,
    dependencies=[Depends(require_owner)],
)
async def replace_admin_role_permissions(
    role_id: UUID,
    payload: RolePermissionBindingUpdate,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> RbacRoleView:
    try:
        result = await service.replace_role_permissions(
            database,
            role_id,
            payload.permission_codes,
            actor,
        )
        await database.commit()
    except AdminRbacError as exc:
        raise _http_error(exc) from exc
    return result


@router.get("/permissions", response_model=RbacPermissionList)
async def list_admin_permissions(database: DatabaseSessionDependency) -> RbacPermissionList:
    items = await service.list_permissions(database)
    return RbacPermissionList(items=items, total=len(items))


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (AdminUserNotFoundError, AdminRoleNotFoundError)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, OwnerPrivilegesRequiredError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(
        exc,
        (DuplicateAdminIdentityError, LastActiveOwnerError, SystemRoleProtectedError),
    ):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, (InvalidRbacBindingError, ValueError)):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="administrator RBAC operation failed",
    )
