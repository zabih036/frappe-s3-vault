from __future__ import annotations

import frappe

FILE_MANAGER_ROLES = [
    "S3 Vault Viewer",
    "S3 Vault Uploader",
    "S3 Vault Manager",
    "S3 Vault Administrator",
]


def ensure_file_manager_roles():
    role_meta = frappe.get_meta("Role")
    for role_name in FILE_MANAGER_ROLES:
        if frappe.db.exists("Role", role_name):
            values = {}
            if role_meta.has_field("desk_access"):
                values["desk_access"] = 1
            if role_meta.has_field("disabled"):
                values["disabled"] = 0
            if values:
                frappe.db.set_value("Role", role_name, values, update_modified=False)
            continue

        values = {
            "doctype": "Role",
            "role_name": role_name,
        }
        if role_meta.has_field("desk_access"):
            values["desk_access"] = 1
        if role_meta.has_field("is_custom"):
            values["is_custom"] = 0
        if role_meta.has_field("disabled"):
            values["disabled"] = 0
        frappe.get_doc(values).insert(ignore_permissions=True)
    frappe.db.commit()


def after_install():
    ensure_file_manager_roles()


def after_migrate():
    ensure_file_manager_roles()


def before_install():
    ensure_file_manager_roles()


def before_migrate():
    ensure_file_manager_roles()
