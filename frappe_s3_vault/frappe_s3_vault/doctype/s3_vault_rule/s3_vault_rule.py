import frappe
from frappe.model.document import Document
from frappe.model.naming import make_autoname
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
        if cint(self.create_attach_field):
            self.validate_dynamic_field()

    def on_update(self):
        if cint(self.create_attach_field):
            self.create_custom_attach_field()

    def validate_dynamic_field(self):
        if not self.dynamic_field_label:
            frappe.throw("Dynamic Field Label is required")
        if not self.dynamic_fieldname:
            frappe.throw("Dynamic Fieldname is required")
        if self.dynamic_fieldtype not in ("Attach", "Attach Image"):
            frappe.throw("Dynamic Fieldtype must be Attach or Attach Image")

    def create_custom_attach_field(self):
        fieldname = self.dynamic_fieldname
        exists = frappe.db.exists("Custom Field", {"dt": self.reference_doctype, "fieldname": fieldname})
        if exists:
            self.db_set("custom_field_created", exists, update_modified=False)
            return

        cf = frappe.get_doc({
            "doctype": "Custom Field",
            "dt": self.reference_doctype,
            "label": self.dynamic_field_label,
            "fieldname": fieldname,
            "fieldtype": self.dynamic_fieldtype or "Attach",
            "insert_after": self.insert_after or None,
        })
        cf.insert(ignore_permissions=True)
        self.db_set("custom_field_created", cf.name, update_modified=False)
        frappe.clear_cache(doctype=self.reference_doctype)
        frappe.msgprint(f"Attach field {fieldname} created in {self.reference_doctype}", indicator="green")
