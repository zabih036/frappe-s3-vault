import os
import urllib.parse

import frappe


DOWNLOAD_PREFIX = "/api/method/frappe_s3_vault.api.download"


def is_secure_url(value):
    return bool(value and str(value).startswith(DOWNLOAD_PREFIX))


def is_local_file_url(value):
    if not value:
        return False

    value = str(value)

    return (
        value.startswith("/private/files/")
        or value.startswith("/files/")
        or "/private/files/" in value
        or "/files/" in value
    )


def normalize_url(value):
    if not value:
        return ""

    value = str(value).strip()
    value = urllib.parse.unquote(value)

    if value.startswith("http://") or value.startswith("https://"):
        try:
            value = urllib.parse.urlparse(value).path
        except Exception:
            pass

    return value


def basename(value):
    value = normalize_url(value)
    return os.path.basename(value.split("?", 1)[0])


def get_doc_value(doc, fieldname):
    try:
        return doc.get(fieldname)
    except Exception:
        return None


def set_doc_value(doc, fieldname, value):
    try:
        if doc.meta.has_field(fieldname):
            doc.set(fieldname, value)
            return True
    except Exception:
        pass

    return False


def get_message_local_url(doc):
    """
    Raven normally stores file URL in file/content.
    """

    for field in ["file", "content", "file_thumbnail", "text"]:
        value = get_doc_value(doc, field)

        if is_secure_url(value):
            return None

        if is_local_file_url(value):
            return normalize_url(value)

    return None


def find_file_for_raven_message(doc, local_url=None):
    """
    Find the File row Raven uploaded before saving/sending Raven Message.
    """

    message_name = doc.name

    # 1. Best: File attached to this Raven Message.
    if message_name:
        rows = frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": "Raven Message",
                "attached_to_name": message_name,
            },
            fields=["name", "file_url", "file_name", "creation"],
            order_by="creation desc",
            limit=5,
        )

        if rows:
            if local_url:
                base = basename(local_url)

                for row in rows:
                    if normalize_url(row.file_url) == normalize_url(local_url):
                        return row.name

                    if base and row.file_name == base:
                        return row.name

                    if base and basename(row.file_url) == base:
                        return row.name

            return rows[0].name

    # 2. Exact local file_url match.
    if local_url:
        file_id = frappe.db.get_value(
            "File",
            {"file_url": normalize_url(local_url)},
            "name",
            order_by="creation desc",
        )

        if file_id:
            return file_id

    # 3. Basename fallback.
    if local_url:
        base = basename(local_url)

        if base:
            file_id = frappe.db.get_value(
                "File",
                {"file_name": base},
                "name",
                order_by="creation desc",
            )

            if file_id:
                return file_id

    return None


def replace_local_values(doc, local_url, final_url):
    changed = []

    for field in ["file", "content", "file_thumbnail", "text", "json", "links"]:
        if not final_url:
            continue

        current = get_doc_value(doc, field)

        if current is None:
            continue

        new_value = str(current)

        if local_url:
            new_value = new_value.replace(str(local_url), final_url)

        base = basename(local_url)

        # If exact field is local file url, force it.
        if is_local_file_url(current):
            new_value = final_url

        # If Raven put only a filename somewhere, do not blindly replace all text,
        # only direct file fields.
        if field in ["file", "content"] and base and str(current).endswith(base):
            new_value = final_url

        if new_value != str(current):
            if set_doc_value(doc, field, new_value):
                changed.append(field)

    # Always force the visible fields for File/Image messages.
    for field in ["file", "content"]:
        if set_doc_value(doc, field, final_url):
            if field not in changed:
                changed.append(field)

    return changed


def prepare_raven_file_before_save(doc, method=None):
    """
    Backend-only Raven fix.

    Before Raven Message is inserted/saved, replace local /private/files URL
    with the final S3 Vault API URL.

    This makes both sender and receiver see API URL from the beginning.
    """

    if getattr(frappe.flags, "s3_vault_skip_raven_prepare", False):
        return

    message_type = str(get_doc_value(doc, "message_type") or "")

    if message_type not in ["Image", "File"]:
        return

    local_url = get_message_local_url(doc)

    # Already secure.
    if not local_url:
        return

    file_id = find_file_for_raven_message(doc, local_url=local_url)

    if not file_id:
        frappe.log_error(
            f"Could not find File row for Raven Message {doc.name} local_url={local_url}",
            "S3 Vault Raven Prepare Missing File",
        )
        return

    try:
        frappe.flags.s3_vault_skip_raven_prepare = True

        from frappe_s3_vault.raven_pre_send import prepare_file

        result = prepare_file(
            file_id=file_id,
            file_url=local_url,
            content=local_url,
        )

        final_url = result.get("url")

        if not final_url:
            frappe.log_error(
                f"prepare_file did not return url for file={file_id}, result={result}",
                "S3 Vault Raven Prepare No URL",
            )
            return

        changed = replace_local_values(doc, local_url, final_url)

        # Publish a small event, but saving correct value is the main fix.
        try:
            frappe.publish_realtime(  # nosemgrep: frappe-realtime-pick-room - intentional app-level UI refresh event.
                "s3_vault_raven_message_prepared",
                {
                    "doctype": "Raven Message",
                    "name": doc.name,
                    "file": file_id,
                    "file_url": final_url,
                    "changed_fields": changed,
                },
                after_commit=True,
            )
        except Exception:
            pass

    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Raven Prepare Failed")
        raise

    finally:
        frappe.flags.s3_vault_skip_raven_prepare = False
