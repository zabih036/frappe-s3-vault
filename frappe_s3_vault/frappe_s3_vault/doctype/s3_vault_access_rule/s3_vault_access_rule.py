from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from frappe_s3_vault.file_manager_permissions import ROLE_LEVELS, normalize_prefix


class S3VaultAccessRule(Document):
    def validate(self):
        self.prefix = normalize_prefix(self.prefix, folder=True)
        if self.principal_type == "Role":
            if not self.role:
                frappe.throw(_("Role is required for a role access rule."))
            self.user = None
        elif self.principal_type == "User":
            if not self.user:
                frappe.throw(_("User is required for a user access rule."))
            self.role = None
        else:
            frappe.throw(_("Principal Type must be Role or User."))

        if self.permission_level not in ROLE_LEVELS:
            frappe.throw(_("Select a valid S3 Vault permission level."))

        filters = {
            "connection": self.connection,
            "prefix": self.prefix,
            "principal_type": self.principal_type,
            "name": ["!=", self.name or ""],
        }
        filters["role" if self.principal_type == "Role" else "user"] = (
            self.role if self.principal_type == "Role" else self.user
        )
        if frappe.db.exists("S3 Vault Access Rule", filters):
            frappe.throw(_("An access rule already exists for this principal, connection, and prefix."))
