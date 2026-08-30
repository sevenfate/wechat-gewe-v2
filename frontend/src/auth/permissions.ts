import type { AuthUser } from "@/api/types";

export function hasPermission(user: AuthUser | null | undefined, permission: string): boolean {
  return Boolean(
    user && (user.roles.includes("owner") || user.permissions.includes(permission)),
  );
}

export function hasAllPermissions(
  user: AuthUser | null | undefined,
  permissions: readonly string[],
): boolean {
  return permissions.every((permission) => hasPermission(user, permission));
}
