import os

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from frappe_s3_vault.utils.s3 import (
    app_enabled,
    assert_document_permission,
    create_log,
    find_rule_for_file,
    get_local_file_path,
    make_object_key,
    s3_client,
    validate_file_against_rule,
)


def handle_file_after_insert(doc, method=None):
    """Upload only files whose attached_to_doctype matches an enabled S3 Vault Rule."""
    if getattr(frappe.flags, "s3_vault_processing", False):
        return
    if not app_enabled():
        return
    if not doc.attached_to_doctype:
        return
    if doc.file_url and doc.file_url.startswith("/api/method/frappe_s3_vault"):
        return
    if frappe.db.exists("S3 Vault File", {"file": doc.name}):
        return

    rule = find_rule_for_file(doc)
    if not rule:
        return  # IMPORTANT: unspecified DocTypes remain local

    upload_file_to_s3(doc, rule)


def upload_file_to_s3(file_doc, rule):
    bucket = frappe.get_doc("S3 Vault Bucket", rule.bucket)
    if not cint(bucket.is_active):
        frappe.throw(_("S3 Vault Bucket {0} is not active").format(bucket.name))

    if cint(rule.require_frappe_permission_check) and file_doc.attached_to_doctype and file_doc.attached_to_name:
        if not frappe.has_permission(file_doc.attached_to_doctype, "read", file_doc.attached_to_name):
            frappe.throw(_("Not permitted to attach file to this document"), frappe.PermissionError)

    content_type = validate_file_against_rule(file_doc, rule)
    local_path = get_local_file_path(file_doc)
    if not local_path or not os.path.exists(local_path):
        frappe.throw(_("Local file not found for upload: {0}").format(file_doc.file_url))

    object_key, stored_name = make_object_key(file_doc, rule, bucket)
    client = s3_client(bucket)

    extra_args = {"ContentType": content_type}
    if cint(rule.is_private):
        extra_args["ACL"] = "private"
    if bucket.storage_class:
        extra_args["StorageClass"] = bucket.storage_class
    if bucket.server_side_encryption and bucket.server_side_encryption != "None":
        extra_args["ServerSideEncryption"] = bucket.server_side_encryption
        if bucket.server_side_encryption == "aws:kms" and bucket.kms_key_id:
            extra_args["SSEKMSKeyId"] = bucket.kms_key_id

    try:
        with open(local_path, "rb") as f:
            client.upload_fileobj(f, bucket.bucket_name, object_key, ExtraArgs=extra_args)
        head = client.head_object(Bucket=bucket.bucket_name, Key=object_key)

        s3_file = frappe.new_doc("S3 Vault File")
        s3_file.file = file_doc.name
        s3_file.file_url = file_doc.file_url
        s3_file.attached_to_doctype = file_doc.attached_to_doctype
        s3_file.attached_to_name = file_doc.attached_to_name
        s3_file.attached_to_field = file_doc.attached_to_field
        s3_file.rule_used = rule.name
        s3_file.bucket = bucket.name
        s3_file.bucket_name = bucket.bucket_name
        s3_file.object_key = object_key
        s3_file.original_file_name = file_doc.file_name
        s3_file.stored_file_name = stored_name
        s3_file.file_size = cint(file_doc.file_size) or cint(head.get("ContentLength"))
        s3_file.content_type = content_type
        s3_file.file_extension = os.path.splitext(file_doc.file_name or "")[1].lower().lstrip(".")
        s3_file.file_hash = getattr(file_doc, "content_hash", None)
        s3_file.etag = (head.get("ETag") or "").strip('"')
        s3_file.version_id = head.get("VersionId")
        s3_file.is_private = cint(rule.is_private)
        s3_file.status = "Uploaded"
        s3_file.uploaded_on = now_datetime()
        s3_file.uploaded_by = frappe.session.user
        s3_file.local_file_url = file_doc.file_url
        s3_file.local_file_deleted = 0
        s3_file.insert(ignore_permissions=True)

        # Change File URL to secure Frappe endpoint. Attachments will now load from Wasabi/S3.
        secure_url = f"/api/method/frappe_s3_vault.api.files.download_file?file={file_doc.name}"
        frappe.flags.s3_vault_processing = True
        frappe.db.set_value("File", file_doc.name, "file_url", secure_url, update_modified=False)
        frappe.flags.s3_vault_processing = False

        if cint(rule.delete_local_after_upload) and cint(rule.keep_local_copy_days or 0) == 0:
            try:
                os.remove(local_path)
                frappe.db.set_value("S3 Vault File", s3_file.name, "local_file_deleted", 1, update_modified=False)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "S3 Vault Local Delete Failed")

        create_log(
            "Upload",
            storage_file=s3_file.name,
            file=file_doc.name,
            doctype_name=file_doc.attached_to_doctype,
            document_name=file_doc.attached_to_name,
            bucket_name=bucket.bucket_name,
            object_key=object_key,
        )
        frappe.db.commit()

    except Exception as e:
        create_log(
            "Upload",
            status="Failed",
            file=file_doc.name,
            doctype_name=file_doc.attached_to_doctype,
            document_name=file_doc.attached_to_name,
            bucket_name=bucket.bucket_name,
            object_key=object_key,
            error_message=str(e),
            traceback=frappe.get_traceback(),
        )
        raise


def handle_file_on_trash(doc, method=None):
    if getattr(frappe.flags, "s3_vault_processing", False):
        return
    if not app_enabled():
        return

    storage_name = frappe.db.get_value("S3 Vault File", {"file": doc.name}, "name")
    if not storage_name:
        return

    storage_file = frappe.get_doc("S3 Vault File", storage_name)
    rule = frappe.get_doc("S3 Vault Rule", storage_file.rule_used) if storage_file.rule_used else None

    if rule and not cint(rule.allow_delete_from_s3):
        frappe.throw(_("Deleting this file from S3 is disabled by S3 Vault Rule"))

    if rule and cint(rule.soft_delete_days or 0) > 0:
        storage_file.status = "Soft Deleted"
        storage_file.deleted_on = now_datetime()
        storage_file.deleted_by = frappe.session.user
        storage_file.save(ignore_permissions=True)
        create_log("Soft Delete", storage_file=storage_file.name, file=doc.name, bucket_name=storage_file.bucket_name, object_key=storage_file.object_key)
        return

    bucket = frappe.get_doc("S3 Vault Bucket", storage_file.bucket)
    client = s3_client(bucket)
    client.delete_object(Bucket=storage_file.bucket_name, Key=storage_file.object_key)
    storage_file.status = "Deleted"
    storage_file.deleted_from_storage = 1 if hasattr(storage_file, "deleted_from_storage") else 0
    storage_file.deleted_on = now_datetime()
    storage_file.deleted_by = frappe.session.user
    storage_file.save(ignore_permissions=True)
    create_log("Delete", storage_file=storage_file.name, file=doc.name, bucket_name=storage_file.bucket_name, object_key=storage_file.object_key)
