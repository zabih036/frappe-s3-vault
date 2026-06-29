import frappe
from frappe.utils import now_datetime

from frappe_s3_vault.utils.s3 import create_log, test_bucket


@frappe.whitelist()
def test_connection(bucket):
    doc = frappe.get_doc("S3 Vault Bucket", bucket)
    try:
        test_bucket(doc, write_test=True)
        doc.last_health_check_status = "Success"
        doc.last_health_check_on = now_datetime()
        doc.save(ignore_permissions=True)
        create_log("Health Check", bucket_name=doc.bucket_name)
        return {"ok": True, "message": "Connection successful"}
    except Exception as e:
        doc.last_health_check_status = "Failed"
        doc.last_health_check_on = now_datetime()
        doc.save(ignore_permissions=True)
        create_log("Health Check", status="Failed", bucket_name=doc.bucket_name, error_message=str(e), traceback=frappe.get_traceback())
        frappe.throw(str(e))
