from __future__ import annotations

import re

import frappe
from frappe import _

ROLE_LEVELS = {
    "S3 Vault Viewer": 10,
    "S3 Vault Uploader": 20,
    "S3 Vault Manager": 30,
    "S3 Vault Administrator": 40,
}
ADMIN_ROLES = {"System Manager", "S3 Vault Administrator"}
INTERNAL_PREFIX = ".s3-vault-temp/"

CAPABILITY_LEVELS = {
    "browse": 10,
    "preview": 10,
    "download": 10,
    "search": 10,
    "dashboard": 10,
    "versions_view": 10,
    "upload": 20,
    "create_folder": 20,
    "multipart_abort_own": 20,
    "copy": 30,
    "move": 30,
    "rename": 30,
    "delete": 30,
    "zip": 30,
    "versions_restore": 30,
    "index_rebuild": 40,
    "operation_cancel": 30,
    "operation_retry": 30,
    "versions_delete": 40,
    "access_admin": 40,
    "index_admin": 40,
}


def normalize_prefix(value: str | None, folder: bool = True) -> str:
    value = str(value or "").strip().replace("\\", "/")
    if "\x00" in value:
        frappe.throw(_("Invalid storage prefix."))
    value = re.sub(r"/+", "/", value).strip("/")
    parts: list[str] = []
    for part in value.split("/"):
        if not part:
            continue
        if part in {".", ".."}:
            frappe.throw(_("Relative path segments are not allowed."))
        parts.append(part)
    normalized = "/".join(parts)
    if folder and normalized:
        normalized += "/"
    return normalized


def user_roles(user: str | None = None) -> set[str]:
    user = user or frappe.session.user
    if not user or user == "Guest":
        return set()
    return set(frappe.get_roles(user))


def global_level(user: str | None = None) -> int:
    user = user or frappe.session.user
    if user == "Administrator":
        return 40
    roles = user_roles(user)
    if roles.intersection(ADMIN_ROLES):
        return 40
    return max((ROLE_LEVELS.get(role, 0) for role in roles), default=0)


def is_admin(user: str | None = None) -> bool:
    return global_level(user) >= 40


def require_page_access(user: str | None = None) -> None:
    if global_level(user) < CAPABILITY_LEVELS["browse"]:
        frappe.throw(
            _("You need an S3 Vault file-manager role to use this page."),
            frappe.PermissionError,
        )


def _matching_rules(connection: str, user: str | None = None) -> list[dict]:
    user = user or frappe.session.user
    roles = user_roles(user)
    if not frappe.db.exists("DocType", "S3 Vault Access Rule"):
        return []

    cache = getattr(frappe.local, "s3_vault_access_rule_cache", None)
    if cache is None:
        cache = {}
        frappe.local.s3_vault_access_rule_cache = cache
    cache_key = (connection, user, tuple(sorted(roles)))
    if cache_key in cache:
        return cache[cache_key]

    rows = frappe.get_all(
        "S3 Vault Access Rule",
        filters={"enabled": 1, "connection": connection},
        fields=[
            "name",
            "connection",
            "prefix",
            "principal_type",
            "user",
            "role",
            "permission_level",
        ],
        order_by="prefix asc",
        limit=10_000,
    )
    output: list[dict] = []
    for row in rows:
        principal_type = str(row.principal_type or "Role")
        if principal_type == "User" and row.user != user:
            continue
        if principal_type == "Role" and row.role not in roles:
            continue
        row = dict(row)
        row["prefix"] = normalize_prefix(row.get("prefix"), folder=True)
        row["level"] = ROLE_LEVELS.get(row.get("permission_level"), 0)
        output.append(row)
    cache[cache_key] = output
    return output


def compact_roots(prefixes: list[str]) -> list[str]:
    roots: list[str] = []
    for prefix in sorted({normalize_prefix(value, folder=True) for value in prefixes}, key=len):
        if any(prefix.startswith(parent) for parent in roots):
            continue
        roots.append(prefix)
    return roots


def accessible_roots(connection: str, user: str | None = None) -> list[str]:
    require_page_access(user)
    if is_admin(user):
        return [""]
    level = global_level(user)
    roots = [
        row["prefix"]
        for row in _matching_rules(connection, user)
        if min(level, int(row.get("level") or 0)) >= CAPABILITY_LEVELS["browse"]
    ]
    return compact_roots(roots)


def effective_level(connection: str, path: str | None, user: str | None = None) -> int:
    require_page_access(user)
    if is_admin(user):
        return 40
    path = normalize_prefix(path, folder=False)
    user_level = global_level(user)
    rule_levels = []
    for row in _matching_rules(connection, user):
        rule_prefix = row["prefix"]
        if not rule_prefix:
            rule_levels.append(int(row.get("level") or 0))
            continue
        bare = rule_prefix.rstrip("/")
        if path == bare or path.startswith(rule_prefix):
            rule_levels.append(int(row.get("level") or 0))
    return min(user_level, max(rule_levels, default=0))


def has_access(
    connection: str,
    path: str | None,
    capability: str,
    user: str | None = None,
) -> bool:
    normalized_path = normalize_prefix(path, folder=False)
    if normalized_path == INTERNAL_PREFIX.rstrip("/") or normalized_path.startswith(INTERNAL_PREFIX):
        return False
    required = CAPABILITY_LEVELS.get(capability)
    if required is None:
        raise ValueError(f"Unknown S3 Vault capability: {capability}")
    return effective_level(connection, normalized_path, user) >= required


def require_access(
    connection: str,
    path: str | None,
    capability: str,
    user: str | None = None,
) -> None:
    if not has_access(connection, path, capability, user):
        frappe.throw(
            _("You do not have {0} access for this S3 location.").format(capability.replace("_", " ")),
            frappe.PermissionError,
        )


def capabilities_for(connection: str, path: str | None, user: str | None = None) -> dict:
    level = effective_level(connection, path, user)
    return {
        capability: level >= required
        for capability, required in CAPABILITY_LEVELS.items()
    } | {"level": level, "is_admin": level >= 40}


def connection_access_payload(connection: str, user: str | None = None) -> dict:
    roots = accessible_roots(connection, user)
    return {
        "access_roots": roots,
        "default_root": roots[0] if roots else None,
        "is_admin": is_admin(user),
        "global_level": global_level(user),
    }


def ensure_paths(
    connection: str,
    paths: list[str],
    capability: str,
    user: str | None = None,
) -> None:
    for path in paths:
        require_access(connection, path, capability, user)


def operation_query_conditions(user: str | None = None) -> str:
    user = user or frappe.session.user
    if is_admin(user):
        return ""
    escaped = frappe.db.escape(user)
    return f"`tabS3 Vault Operation`.`started_by`={escaped}"


def operation_has_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool:
    user = user or frappe.session.user
    return bool(is_admin(user) or getattr(doc, "started_by", None) == user)


def multipart_query_conditions(user: str | None = None) -> str:
    user = user or frappe.session.user
    if is_admin(user):
        return ""
    escaped = frappe.db.escape(user)
    return f"`tabS3 Vault Multipart Upload`.`upload_user`={escaped}"


def multipart_has_permission(doc, user: str | None = None, permission_type: str | None = None) -> bool:
    user = user or frappe.session.user
    return bool(is_admin(user) or getattr(doc, "upload_user", None) == user)
