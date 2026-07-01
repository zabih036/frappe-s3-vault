import frappe
from frappe.model.document import Document
from frappe.utils import cint


class S3VaultRule(Document):
    def validate(self):
        if not self.reference_doctype:
            frappe.throw("Reference DocType is required")

        if not self.bucket:
            frappe.throw("Bucket is required")

        if not self.folder_pattern:
            self.folder_pattern = "{site}/{doctype}/{docname}/{yyyy}/{mm}"

        if not self.filename_strategy:
            self.filename_strategy = "Hash Prefix"

        if not self.url_expiry_seconds:
            self.url_expiry_seconds = 900

        if self.applies_to == "Specific Attach Field" and not self.attach_fieldname:
            frappe.throw("Attach Fieldname is required when Applies To is Specific Attach Field")

        if cint(self.max_file_size_mb) < 0:
            frappe.throw("Max File Size MB cannot be negative")

        if cint(self.url_expiry_seconds) < 0:
            frappe.throw("URL Expiry Seconds cannot be negative")

        if cint(self.soft_delete_days) < 0:
            frappe.throw("Soft Delete Days cannot be negative")

        if cint(self.keep_local_copy_days) < 0:
            frappe.throw("Keep Local Copy Days cannot be negative")
