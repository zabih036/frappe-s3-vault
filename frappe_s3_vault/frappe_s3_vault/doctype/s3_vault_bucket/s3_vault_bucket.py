import frappe
from frappe.model.document import Document

class S3VaultBucket(Document):
    def validate(self):
        if self.bucket_name and self.access_key and self.secret_key:
            try:
                from frappe_s3_vault.api import test_connection
                test_connection(self.name)
                frappe.msgprint("S3 connection successful")
            except Exception as e:
                frappe.msgprint(f"S3 connection failed: {str(e)}")
