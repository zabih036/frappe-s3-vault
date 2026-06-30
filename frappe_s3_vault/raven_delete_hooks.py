import re
import frappe


DOWNLOAD_PREFIX = "/api/method/frappe_s3_vault.api.download"


def extract_file_ids_from_text(value):
    if not value:
        return []

    value = str(value)

    ids = set()

    for match in re.findall(r"frappe_s3_vault\.api\.download\?file=([A-Za-z0-9]+)", value):
        ids.add(match)

    for match in re.findall(r"/api/method/frappe_s3_vault\.api\.download\?file=([A-Za-z0-9]+)", value):
        ids.add(match)

    return list(ids)


def get_message_text_values(doc):
    values = []

    for field in [
        "file",
        "file_thumbnail",
        "content",
        "text",
        "json",
        "links",
        "blurhash",
    ]:
        try:
            values.append(doc.get(field))
        except Exception:
            pass

    return values


def get_file_ids_from_raven_message(doc):
    """
    Find all Frappe File IDs related to this Raven Message.
    """

    ids = set()

    message_name = doc.name

    # 1. File rows attached to this Raven Message.
    rows = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "Raven Message",
            "attached_to_name": message_name,
        },
        fields=["name", "file_url"],
    )

    for row in rows:
        ids.add(row.name)
        for found in extract_file_ids_from_text(row.file_url):
            ids.add(found)

    # 2. S3 Vault rows attached to this Raven Message.
    s3_rows = frappe.get_all(
        "S3 Vault File",
        filters={
            "attached_to_doctype": "Raven Message",
            "attached_to_name": message_name,
        },
        fields=["file", "object_key", "status"],
    )

    for row in s3_rows:
        if row.file:
            ids.add(row.file)

    # 3. Values inside Raven Message fields.
    for value in get_message_text_values(doc):
        for found in extract_file_ids_from_text(value):
            ids.add(found)

    return [x for x in ids if x]


def raven_message_still_has_file_card(doc):
    """
    True means Raven UI probably still renders a file/image card.
    """

    message_type = str(doc.get("message_type") or "").lower()

    if message_type in ["image", "file", "video", "audio", "attachment"]:
        return True

    for field in ["file", "file_thumbnail"]:
        if doc.get(field):
            return True

    content = str(doc.get("content") or "")
    if "frappe_s3_vault.api.download?file=" in content:
        return True

    return False


def cleanup_raven_message_files(doc, reason):
    """
    Cleanup all uploaded files related to this Raven Message.
    """

    if getattr(frappe.flags, "s3_vault_skip_raven_delete_hook", False):
        return

    file_ids = get_file_ids_from_raven_message(doc)

    if not file_ids:
        return

    from frappe_s3_vault.delete_cleanup import finalize_deleted_file

    for file_id in file_ids:
        try:
            finalize_deleted_file(
                file_id=file_id,
                dry_run=0,
                delete_file_record=1,
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"S3 Vault Raven cleanup failed: {reason}",
            )


def on_trash_raven_message(doc, method=None):
    """
    If a Raven Message is deleted, cleanup related S3/File records.
    """

    cleanup_raven_message_files(doc, "Raven Message deleted")


def on_update_raven_message(doc, method=None):
    """
    If Raven UI changes/removes file card metadata, cleanup related S3/File records.

    This is intentionally conservative:
    - If the message still has a file card, do nothing.
    - If the message no longer has a file card but S3/File rows exist, cleanup.
    """

    if raven_message_still_has_file_card(doc):
        return

    cleanup_raven_message_files(doc, "Raven Message file card removed")
