import frappe


def publish_file_uploaded(file_id, storage_file=None, object_key=None, bucket_name=None):
    """
    Notify browsers that File.file_url changed to S3 Vault API URL.

    Important:
    Do not restrict by user, because upload runs in background worker and
    frappe.session.user may be Administrator instead of the real browser user.
    """

    if not file_id or not frappe.db.exists("File", file_id):
        return {
            "status": "skipped",
            "reason": "File not found",
            "file": file_id,
        }

    file_doc = frappe.get_doc("File", file_id)

    payload = {
        "file": file_id,
        "file_name": file_doc.file_name,
        "file_url": file_doc.file_url,
        "attached_to_doctype": file_doc.attached_to_doctype,
        "attached_to_name": file_doc.attached_to_name,
        "attached_to_field": file_doc.attached_to_field,
        "storage_file": storage_file,
        "bucket_name": bucket_name,
        "object_key": object_key,
    }

    # Broadcast custom event to all active clients.
    frappe.publish_realtime(
        "s3_vault_file_uploaded",
        payload,
        after_commit=True,
    )

    # Extra Raven-specific event.
    if file_doc.attached_to_doctype == "Raven Message":
        frappe.publish_realtime(
            "s3_vault_raven_message_uploaded",
            payload,
            after_commit=True,
        )

    # Standard doc_update event.
    if file_doc.attached_to_doctype and file_doc.attached_to_name:
        frappe.publish_realtime(
            "doc_update",
            {
                "doctype": file_doc.attached_to_doctype,
                "name": file_doc.attached_to_name,
            },
            after_commit=True,
        )

    return {
        "status": "published",
        "payload": payload,
    }
