import frappe


DOWNLOAD_PREFIX = "/api/method/frappe_s3_vault.api.download"


def raven_has_field(fieldname):
    try:
        return frappe.get_meta("Raven Message").has_field(fieldname)
    except Exception:
        return False


def sql_set(message_name, fieldname, value):
    if not raven_has_field(fieldname):
        return False

    frappe.db.set_value("Raven Message", message_name, fieldname, value, update_modified=False)
    return True


def get_raven_message(file_doc):
    if not file_doc:
        return None

    if file_doc.attached_to_doctype != "Raven Message":
        return None

    if not file_doc.attached_to_name:
        return None

    if not frappe.db.exists("Raven Message", file_doc.attached_to_name):
        return None

    return frappe.get_doc("Raven Message", file_doc.attached_to_name)


def replace_text(value, old_values, new_url):
    if value is None:
        return value

    new_value = str(value)

    for old in old_values:
        if old and str(old) in new_value:
            new_value = new_value.replace(str(old), new_url)

    return new_value


def sync_raven_message_after_upload(file_id, new_url=None, commit=False):
    """
    After S3 upload, update Raven Message DB fields to use S3 Vault API URL.

    This does not depend on Desk form JS.
    """

    if not file_id or not frappe.db.exists("File", file_id):
        return {
            "status": "skipped",
            "reason": "File not found",
            "file": file_id,
        }

    file_doc = frappe.get_doc("File", file_id)
    msg = get_raven_message(file_doc)

    if not msg:
        return {
            "status": "skipped",
            "reason": "Not a Raven Message file",
            "file": file_id,
        }

    message_name = msg.name
    new_url = new_url or f"{DOWNLOAD_PREFIX}?file={file_id}"

    old_values = [
        file_doc.file_url,
        file_doc.file_name,
        f"/files/{file_doc.file_name}",
        f"/private/files/{file_doc.file_name}",
    ]

    changed = []

    # Force important visible fields.
    for field in ["file", "content"]:
        if sql_set(message_name, field, new_url):
            changed.append(field)

    # Replace in metadata fields.
    for field in ["file_thumbnail", "text", "json", "links"]:
        if not raven_has_field(field):
            continue

        current = frappe.db.get_value("Raven Message", message_name, field)

        if current is None:
            continue

        new_value = replace_text(current, old_values, new_url)

        if new_value != str(current):
            sql_set(message_name, field, new_value)
            changed.append(field)

    # Keep message type as image/file if Raven already set it.
    # Do not convert to Text here.

    payload = {
        "doctype": "Raven Message",
        "name": message_name,
        "file": file_id,
        "file_url": new_url,
        "content": new_url,
        "message_type": frappe.db.get_value("Raven Message", message_name, "message_type"),
        "channel_id": frappe.db.get_value("Raven Message", message_name, "channel_id"),
        "changed_fields": changed,
    }

    # Publish multiple events because Raven frontend may listen to one of them.
    for event_name in [
        "s3_vault_raven_message_updated",
        "raven_message_updated",
        "message_updated",
        "doc_update",
    ]:
        try:
            frappe.publish_realtime(
                event_name,
                payload,
                after_commit=True,
            )
        except Exception:
            pass

    if commit:
        frappe.db.commit()

    return {
        "status": "updated",
        "message": message_name,
        "file": file_id,
        "url": new_url,
        "changed_fields": changed,
    }


# final override: always read latest File.file_url from database
def sync_raven_message_after_upload(file_id, new_url=None, commit=False):
    """
    After S3 upload, update Raven Message DB fields to use latest File.file_url.

    Important:
    upload.py may still hold old in-memory file_doc.file_url, so this function
    always reads the latest URL from tabFile.
    """

    if not file_id or not frappe.db.exists("File", file_id):
        return {
            "status": "skipped",
            "reason": "File not found",
            "file": file_id,
        }

    file_doc = frappe.get_doc("File", file_id)

    if file_doc.attached_to_doctype != "Raven Message" or not file_doc.attached_to_name:
        return {
            "status": "skipped",
            "reason": "Not a Raven Message file",
            "file": file_id,
        }

    message_name = file_doc.attached_to_name

    if not frappe.db.exists("Raven Message", message_name):
        return {
            "status": "skipped",
            "reason": "Raven Message not found",
            "file": file_id,
            "message": message_name,
        }

    latest_file_url = frappe.db.get_value("File", file_id, "file_url")
    secure_url = f"{DOWNLOAD_PREFIX}?file={file_id}"

    new_url = new_url or latest_file_url or secure_url

    # Safety: if caller passed old local URL, force secure URL.
    if not str(new_url).startswith(DOWNLOAD_PREFIX):
        new_url = secure_url

    old_values = [
        file_doc.file_url,
        latest_file_url,
        file_doc.file_name,
        f"/files/{file_doc.file_name}",
        f"/private/files/{file_doc.file_name}",
    ]

    # Also include current Raven values as replace targets if they are local paths.
    for field in ["file", "content", "file_thumbnail", "text", "json", "links"]:
        try:
            current = frappe.db.get_value("Raven Message", message_name, field)
            if current and str(current).startswith(("/files/", "/private/files/")):
                old_values.append(current)
        except Exception:
            pass

    old_values = [x for x in old_values if x]

    changed = []

    # Force direct visible fields.
    for field in ["file", "content"]:
        if sql_set(message_name, field, new_url):
            changed.append(field)

    # Replace in metadata fields.
    for field in ["file_thumbnail", "text", "json", "links"]:
        if not raven_has_field(field):
            continue

        current = frappe.db.get_value("Raven Message", message_name, field)

        if current is None:
            continue

        new_value = replace_text(current, old_values, new_url)

        if new_value != str(current):
            sql_set(message_name, field, new_value)
            changed.append(field)

    payload = {
        "doctype": "Raven Message",
        "name": message_name,
        "file": file_id,
        "file_url": new_url,
        "content": new_url,
        "message_type": frappe.db.get_value("Raven Message", message_name, "message_type"),
        "channel_id": frappe.db.get_value("Raven Message", message_name, "channel_id"),
        "changed_fields": changed,
    }

    for event_name in [
        "s3_vault_raven_message_updated",
        "raven_message_updated",
        "message_updated",
        "doc_update",
    ]:
        try:
            frappe.publish_realtime(
                event_name,
                payload,
                after_commit=True,
            )
        except Exception:
            pass

    if commit:
        frappe.db.commit()

    return {
        "status": "updated",
        "message": message_name,
        "file": file_id,
        "url": new_url,
        "changed_fields": changed,
    }
