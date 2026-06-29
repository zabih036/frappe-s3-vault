import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from frappe_s3_vault.utils.s3 import (
    assert_document_permission,
    create_log,
    get_settings,
    get_storage_file_by_file,
    s3_client,
)


@frappe.whitelist()
def download_file(file):
    """Permission-checked download/preview URL for files stored in Wasabi/S3."""
    storage_file = get_storage_file_by_file(file)
    if not storage_file:
        frappe.throw(_("S3 Vault File record not found"))

    rule = frappe.get_doc("S3 Vault Rule", storage_file.rule_used) if storage_file.rule_used else None
    if rule and cint(rule.require_frappe_permission_check):
        assert_document_permission(storage_file, "read")

    if rule and not cint(rule.allow_download):
        frappe.throw(_("Download is disabled for this DocType"), frappe.PermissionError)

    bucket = frappe.get_doc("S3 Vault Bucket", storage_file.bucket)
    client = s3_client(bucket)

    settings = get_settings()
    expiry = cint(rule.url_expiry_seconds if rule else 0) or cint(getattr(settings, "default_url_expiry_seconds", 0)) or 900

    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": storage_file.bucket_name, "Key": storage_file.object_key},
        ExpiresIn=expiry,
    )

    storage_file.last_accessed_on = now_datetime()
    storage_file.save(ignore_permissions=True)

    create_log(
        "Generate URL",
        storage_file=storage_file.name,
        file=file,
        doctype_name=storage_file.attached_to_doctype,
        document_name=storage_file.attached_to_name,
        bucket_name=storage_file.bucket_name,
        object_key=storage_file.object_key,
    )

    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = url


@frappe.whitelist()
def get_file_url(file):
    """Returns a short-lived URL. Use this from custom UI if you do not want redirect."""
    storage_file = get_storage_file_by_file(file)
    if not storage_file:
        frappe.throw(_("S3 Vault File record not found"))

    rule = frappe.get_doc("S3 Vault Rule", storage_file.rule_used) if storage_file.rule_used else None
    if rule and cint(rule.require_frappe_permission_check):
        assert_document_permission(storage_file, "read")

    bucket = frappe.get_doc("S3 Vault Bucket", storage_file.bucket)
    client = s3_client(bucket)
    settings = get_settings()
    expiry = cint(rule.url_expiry_seconds if rule else 0) or cint(getattr(settings, "default_url_expiry_seconds", 0)) or 900

    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": storage_file.bucket_name, "Key": storage_file.object_key},
        ExpiresIn=expiry,
    )
