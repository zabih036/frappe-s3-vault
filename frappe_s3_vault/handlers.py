import os
from urllib.parse import unquote

import frappe


DOWNLOAD_PREFIX = "/api/method/frappe_s3_vault.api.download"


def secure_url(file_id):
    return f"{DOWNLOAD_PREFIX}?file={file_id}"


def safe_has_field(doctype, fieldname):
    try:
        return frappe.get_meta(doctype).has_field(fieldname)
    except Exception:
        return False


def safe_set(doc, fieldname, value):
    if doc.meta.has_field(fieldname):
        doc.set(fieldname, value)


def safe_log(action, status="Success", file_id=None, message=None):
    try:
        doc = frappe.new_doc("S3 Vault Log")

        if doc.meta.has_field("action"):
            doc.action = action

        if doc.meta.has_field("status"):
            doc.status = status

        if doc.meta.has_field("file") and file_id and frappe.db.exists("File", file_id):
            doc.file = file_id

        if doc.meta.has_field("error_message"):
            doc.error_message = message

        if doc.meta.has_field("message"):
            doc.message = message

        if doc.meta.has_field("details"):
            doc.details = message

        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)

    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Log Failed")


def is_s3_url(file_url):
    return bool(file_url and str(file_url).startswith(DOWNLOAD_PREFIX))


def local_path_from_file_url(file_url):
    if not file_url:
        return None

    file_url = unquote(str(file_url))

    if file_url.startswith("/private/files/"):
        return frappe.get_site_path("private", "files", os.path.basename(file_url))

    if file_url.startswith("/files/"):
        return frappe.get_site_path("public", "files", os.path.basename(file_url))

    return None


def get_file_local_path(file_doc):
    # Important: use the original file_url before changing it to API URL.
    path = local_path_from_file_url(file_doc.file_url)

    if path:
        return path

    try:
        from frappe_s3_vault.utils import file_path
        return file_path(file_doc)
    except Exception:
        return None


def get_or_create_vault_file(file_doc):
    existing = frappe.db.get_value(
        "S3 Vault File",
        {"file": file_doc.name},
        "name",
        order_by="creation desc",
    )

    if existing:
        return frappe.get_doc("S3 Vault File", existing)

    doc = frappe.new_doc("S3 Vault File")
    safe_set(doc, "file", file_doc.name)
    return doc


def delete_local_file_after_upload(path):
    if path and os.path.isfile(path):
        os.remove(path)
        return True

    return False


def repair_raven_url(file_id):
    try:
        from frappe_s3_vault.raven_repair import repair_raven_for_file
        repair_raven_for_file(file_id)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Raven Repair Failed")


def should_delete_local(rule_doc):
    try:
        delete_now = int(getattr(rule_doc, "delete_local_after_upload", 0) or 0)
        keep_days = int(getattr(rule_doc, "keep_local_copy_days", 0) or 0)
        return delete_now and keep_days == 0
    except Exception:
        return False


def enqueue_s3_upload(file_doc, method=None):
    try:
        from frappe_s3_vault.utils import enabled_rule_for_file

        if is_s3_url(file_doc.file_url):
            return

        rule = enabled_rule_for_file(file_doc)
        if not rule:
            return

        frappe.enqueue(
            "frappe_s3_vault.handlers.upload_file_to_s3",
            queue="long",
            file_name=file_doc.name,
            enqueue_after_commit=True,
            job_id=f"s3-vault-upload-{file_doc.name}",
            deduplicate=True,
        )

    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Enqueue Failed")


def after_insert_file(doc, method=None):
    enqueue_s3_upload(doc)


def on_update_file(doc, method=None):
    enqueue_s3_upload(doc)


def upload_file_to_s3(file_name):
    try:
        from frappe_s3_vault.utils import (
            enabled_rule_for_file,
            validate_file_against_rule,
            make_object_key,
            s3_client,
            sha256,
        )

        if not frappe.db.exists("File", file_name):
            return f"File not found: {file_name}"

        file_doc = frappe.get_doc("File", file_name)

        if is_s3_url(file_doc.file_url):
            return f"Already uploaded: {file_name}"

        rule_doc = enabled_rule_for_file(file_doc)
        if not rule_doc:
            return f"No S3 Vault Rule for: {file_name}"

        validate_file_against_rule(file_doc, rule_doc)

        local_path = get_file_local_path(file_doc)
        original_file_url = file_doc.file_url

        if not local_path or not os.path.isfile(local_path):
            raise FileNotFoundError(local_path or original_file_url or file_doc.name)

        bucket_doc = frappe.get_doc("S3 Vault Bucket", rule_doc.bucket)

        try:
            object_key = make_object_key(file_doc, rule_doc, bucket_doc)
        except TypeError:
            object_key = make_object_key(file_doc, rule_doc)

        client = s3_client(bucket_doc)

        extra_args = {}
        if getattr(rule_doc, "is_private", 1):
            extra_args["ACL"] = "private"

        client.upload_file(
            Filename=local_path,
            Bucket=bucket_doc.bucket_name,
            Key=object_key,
            ExtraArgs=extra_args,
        )

        file_hash = None
        try:
            file_hash = sha256(local_path)
        except Exception:
            pass

        vault = get_or_create_vault_file(file_doc)

        safe_set(vault, "file", file_doc.name)
        safe_set(vault, "bucket", bucket_doc.name)
        safe_set(vault, "object_key", object_key)
        safe_set(vault, "status", "Uploaded")
        safe_set(vault, "local_file_url", original_file_url)
        safe_set(vault, "file_url", secure_url(file_doc.name))
        safe_set(vault, "file_hash", file_hash)
        safe_set(vault, "local_file_deleted", 0)

        vault.flags.ignore_permissions = True

        if vault.is_new():
            vault.insert(ignore_permissions=True)
        else:
            vault.save(ignore_permissions=True)

        # Force File URL to API only after upload succeeds.
        frappe.db.set_value(
            "File",
            file_doc.name,
            "file_url",
            secure_url(file_doc.name),
            update_modified=False,
        )

        # Repair Raven stored JSON/text URLs.
        if file_doc.attached_to_doctype == "Raven Message":
            repair_raven_url(file_doc.name)

        local_deleted = False

        if should_delete_local(rule_doc):
            local_deleted = delete_local_file_after_upload(local_path)

        if local_deleted:
            frappe.db.set_value(
                "S3 Vault File",
                vault.name,
                "local_file_deleted",
                1,
                update_modified=False,
            )

        safe_log(
            "Upload",
            "Success",
            file_doc.name,
            f"Uploaded to {object_key}; local_deleted={int(local_deleted)}",
        )

        frappe.db.commit()
        return f"Uploaded {file_doc.name} to {object_key}; local_deleted={int(local_deleted)}"

    except Exception:
        safe_log("Upload", "Failed", file_name, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "S3 Vault Upload Failed")
        raise


def release_file_links(file_id):
    # Release links so File deletion is not blocked.
    try:
        frappe.db.sql(
            "update `tabS3 Vault File` set file=NULL where file=%s",
            file_id,
        )
    except Exception:
        try:
            frappe.db.sql(
                "update `tabS3 Vault File` set file='' where file=%s",
                file_id,
            )
        except Exception:
            pass

    try:
        frappe.db.sql(
            "update `tabS3 Vault Log` set file=NULL where file=%s",
            file_id,
        )
    except Exception:
        try:
            frappe.db.sql(
                "update `tabS3 Vault Log` set file='' where file=%s",
                file_id,
            )
        except Exception:
            pass


def clean_raven_deleted_reference(file_id):
    try:
        from frappe_s3_vault.delete_fix import clean_raven_message_for_deleted_file
        clean_raven_message_for_deleted_file(file_id)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Raven Delete Cleanup Failed")


def on_trash_file(doc, method=None):
    file_id = doc.name

    try:
        from frappe_s3_vault.utils import s3_client

        rows = frappe.get_all(
            "S3 Vault File",
            filters={"file": file_id},
            fields=["name", "bucket", "object_key", "status"],
            order_by="creation desc",
        )

        for row in rows:
            if row.object_key and row.bucket:
                try:
                    bucket_doc = frappe.get_doc("S3 Vault Bucket", row.bucket)
                    client = s3_client(bucket_doc)

                    try:
                        client.delete_object(
                            Bucket=bucket_doc.bucket_name,
                            Key=row.object_key,
                        )
                    except Exception:
                        # If object already missing, still mark deleted.
                        pass

                    frappe.db.set_value(
                        "S3 Vault File",
                        row.name,
                        "status",
                        "Deleted",
                        update_modified=False,
                    )

                    if safe_has_field("S3 Vault File", "deleted_from_storage"):
                        frappe.db.set_value(
                            "S3 Vault File",
                            row.name,
                            "deleted_from_storage",
                            1,
                            update_modified=False,
                        )

                    if safe_has_field("S3 Vault File", "deleted_on"):
                        frappe.db.set_value(
                            "S3 Vault File",
                            row.name,
                            "deleted_on",
                            frappe.utils.now(),
                            update_modified=False,
                        )

                    if safe_has_field("S3 Vault File", "deleted_by"):
                        frappe.db.set_value(
                            "S3 Vault File",
                            row.name,
                            "deleted_by",
                            frappe.session.user,
                            update_modified=False,
                        )

                except Exception:
                    frappe.log_error(frappe.get_traceback(), "S3 Vault Delete Object Failed")

        clean_raven_deleted_reference(file_id)

        safe_log(
            "Delete",
            "Success",
            file_id,
            f"Deleted file_id={file_id} from storage and cleaned references",
        )

        release_file_links(file_id)

        frappe.db.commit()

    except Exception:
        safe_log("Delete", "Failed", file_id, frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "S3 Vault File Delete Failed")
