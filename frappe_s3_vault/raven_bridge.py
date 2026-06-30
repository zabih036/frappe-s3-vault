import os
import urllib.parse

import frappe


DOWNLOAD_PREFIX = "/api/method/frappe_s3_vault.api.download"


def secure_url(file_id):
    return f"{DOWNLOAD_PREFIX}?file={file_id}"


def normalize_url(value):
    if not value:
        return ""

    value = str(value).strip()
    value = urllib.parse.unquote(value)

    if value.startswith("http://") or value.startswith("https://"):
        try:
            parsed = urllib.parse.urlparse(value)
            value = parsed.path
        except Exception:
            pass

    return value


def basename(value):
    value = normalize_url(value)
    return os.path.basename(value)


def get_uploaded_vault_by_file(file_id):
    if not file_id:
        return None

    name = frappe.db.get_value(
        "S3 Vault File",
        {
            "file": file_id,
            "status": "Uploaded",
        },
        "name",
        order_by="creation desc",
    )

    if name:
        return frappe.get_doc("S3 Vault File", name)

    return None


def resolve_from_file(file_id):
    if not file_id or not frappe.db.exists("File", file_id):
        return None

    file_url = frappe.db.get_value("File", file_id, "file_url")

    if file_url and str(file_url).startswith(DOWNLOAD_PREFIX):
        return {
            "status": "resolved",
            "file": file_id,
            "url": file_url,
            "source": "File.file_url",
        }

    vault = get_uploaded_vault_by_file(file_id)

    if vault:
        return {
            "status": "resolved",
            "file": file_id,
            "url": secure_url(file_id),
            "storage_file": vault.name,
            "source": "S3 Vault File",
        }

    return None


def resolve_from_local_url(local_url):
    local_url = normalize_url(local_url)

    if not local_url:
        return None

    if local_url.startswith(DOWNLOAD_PREFIX):
        file_id = local_url.split("file=", 1)[-1]
        return resolve_from_file(file_id) or {
            "status": "resolved",
            "file": file_id,
            "url": local_url,
            "source": "already_secure",
        }

    # 1. Match old local URL stored in S3 Vault File.
    candidates = frappe.get_all(
        "S3 Vault File",
        filters={"local_file_url": local_url},
        fields=["name", "file", "status", "object_key"],
        order_by="creation desc",
        limit=1,
    )

    if candidates and candidates[0].file:
        resolved = resolve_from_file(candidates[0].file)
        if resolved:
            resolved["storage_file"] = candidates[0].name
            resolved["source"] = "S3 Vault File.local_file_url"
            return resolved

    # 2. Match by old File.file_url, if still exists.
    file_id = frappe.db.get_value("File", {"file_url": local_url}, "name")

    if file_id:
        resolved = resolve_from_file(file_id)
        if resolved:
            resolved["source"] = "File.old_file_url"
            return resolved

    # 3. Match by basename for Raven cases.
    base = basename(local_url)

    if base:
        rows = frappe.db.sql(
            """
            select name, file, local_file_url, original_file_name, stored_file_name, object_key
            from `tabS3 Vault File`
            where status='Uploaded'
              and file is not null
              and (
                    local_file_url like %s
                    or original_file_name=%s
                    or stored_file_name=%s
                    or object_key like %s
              )
            order by creation desc
            limit 1
            """,
            (f"%/{base}", base, base, f"%/{base}"),
            as_dict=True,
        )

        if rows and rows[0].file:
            resolved = resolve_from_file(rows[0].file)
            if resolved:
                resolved["storage_file"] = rows[0].name
                resolved["source"] = "basename_match"
                return resolved

    return None


def resolve_from_raven_message(message_name):
    if not message_name or not frappe.db.exists("Raven Message", message_name):
        return None

    file_id = frappe.db.get_value(
        "File",
        {
            "attached_to_doctype": "Raven Message",
            "attached_to_name": message_name,
        },
        "name",
        order_by="creation desc",
    )

    if file_id:
        resolved = resolve_from_file(file_id)
        if resolved:
            resolved["message"] = message_name
            resolved["source"] = "Raven Message attached File"
            return resolved

    return None


@frappe.whitelist()
def resolve_file_url(local_url=None, file_id=None, message_name=None):
    """
    Resolve stale Raven local /private/files/... URL to latest S3 Vault API URL.
    """

    result = None

    if file_id:
        result = resolve_from_file(file_id)

    if not result and message_name:
        result = resolve_from_raven_message(message_name)

    if not result and local_url:
        result = resolve_from_local_url(local_url)

    if not result:
        return {
            "status": "not_found",
            "local_url": local_url,
            "file": file_id,
            "message": message_name,
        }

    return result
