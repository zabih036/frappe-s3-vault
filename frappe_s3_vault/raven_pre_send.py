import re
import frappe


DOWNLOAD_PREFIX = "/api/method/frappe_s3_vault.api.download"


def secure_url(file_id):
    return f"{DOWNLOAD_PREFIX}?file={file_id}"


def extract_file_id(value):
    if not value:
        return None

    value = str(value)

    if "frappe_s3_vault.api.download?file=" in value:
        return value.split("file=", 1)[-1].split("&", 1)[0]

    # Try exact File.file_url match.
    file_id = frappe.db.get_value("File", {"file_url": value}, "name")
    if file_id:
        return file_id

    # Try basename match for local paths.
    basename = value.split("?")[0].rstrip("/").split("/")[-1]

    if basename:
        file_id = frappe.db.get_value("File", {"file_name": basename}, "name", order_by="creation desc")
        if file_id:
            return file_id

    return None


def get_file_id_from_args(file_id=None, file_url=None, content=None, file=None):
    return (
        file_id
        or extract_file_id(file)
        or extract_file_id(file_url)
        or extract_file_id(content)
    )


@frappe.whitelist()
def prepare_file(file_id=None, file_url=None, content=None, file=None):
    """
    Raven frontend should call this after local file upload but before saving/sending message.

    It uploads the File to S3 Vault if not already uploaded and returns the final secure API URL.
    """

    file_id = get_file_id_from_args(
        file_id=file_id,
        file_url=file_url,
        content=content,
        file=file,
    )

    if not file_id or not frappe.db.exists("File", file_id):
        return {
            "status": "not_found",
            "file": file_id,
            "file_url": file_url,
            "content": content,
        }

    current_url = frappe.db.get_value("File", file_id, "file_url")

    if current_url and str(current_url).startswith(DOWNLOAD_PREFIX):
        return {
            "status": "already_ready",
            "file": file_id,
            "url": current_url,
        }

    # Run S3 upload now. This is safe because Frappe upload request has already finished.
    from frappe_s3_vault.upload import upload_file_to_s3

    try:
        upload_file_to_s3(file_id)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Raven Pre-send Upload Failed")
        raise

    final_url = frappe.db.get_value("File", file_id, "file_url") or secure_url(file_id)

    # If upload did not update File.file_url for some reason, force it.
    if not str(final_url).startswith(DOWNLOAD_PREFIX):
        final_url = secure_url(file_id)
        frappe.db.set_value("File", file_id, "file_url", final_url, update_modified=False)

    # If Raven Message already exists, sync it too.
    try:
        from frappe_s3_vault.raven_sync import sync_raven_message_after_upload
        sync_raven_message_after_upload(file_id, new_url=final_url, commit=False)
    except Exception:
        pass

    frappe.db.commit()

    return {
        "status": "ready",
        "file": file_id,
        "url": final_url,
    }


@frappe.whitelist()
def prepare_text(text):
    """
    Replace any local file URLs inside Raven message text/content before save.
    """

    if not text:
        return {
            "status": "empty",
            "text": text,
        }

    output = str(text)

    local_urls = set(re.findall(r"(/private/files/[^\\s\"'<>]+|/files/[^\\s\"'<>]+)", output))

    replaced = []

    for local_url in local_urls:
        result = prepare_file(file_url=local_url)

        if result.get("url"):
            output = output.replace(local_url, result["url"])
            replaced.append(
                {
                    "old": local_url,
                    "new": result["url"],
                    "file": result.get("file"),
                }
            )

    return {
        "status": "ready",
        "text": output,
        "replaced": replaced,
    }


# stable override: Raven must respect disabled rules and extension validation before message save
@frappe.whitelist()
def prepare_file(file_id=None, file=None, file_url=None, *args, **kwargs):
    import frappe

    file_id = file_id or file

    if not file_id and file_url:
        file_id = frappe.db.get_value("File", {"file_url": file_url}, "name")

    if not file_id or not frappe.db.exists("File", file_id):
        frappe.throw("Raven file was not found")

    file_doc = frappe.get_doc("File", file_id)

    from frappe_s3_vault.utils import enabled_rule_for_file, validate_file_against_rule

    rule_doc = enabled_rule_for_file(file_doc)

    # If rule is disabled/no rule matched, do not upload to S3 and do not convert to Wasabi API URL.
    if not rule_doc:
        return {
            "status": "rule_disabled",
            "file": file_doc.name,
            "url": file_doc.file_url,
        }

    # Validate extension before Raven message is saved.
    validate_file_against_rule(file_doc, rule_doc)

    secure_url = f"/api/method/frappe_s3_vault.api.download?file={file_doc.name}"

    if file_doc.file_url == secure_url:
        return {
            "status": "already_ready",
            "file": file_doc.name,
            "url": secure_url,
        }

    uploaded = frappe.get_all(
        "S3 Vault File",
        filters={
            "file": file_doc.name,
            "status": "Uploaded",
        },
        fields=["name", "object_key"],
        limit=1,
        order_by="creation desc",
    )

    if uploaded:
        frappe.db.set_value(
            "File",
            file_doc.name,
            "file_url",
            secure_url,
            update_modified=False,
        )

        return {
            "status": "already_uploaded",
            "file": file_doc.name,
            "url": secure_url,
        }

    from frappe_s3_vault.upload import upload_file_to_s3

    result = upload_file_to_s3(file_doc.name)

    file_url = frappe.db.get_value("File", file_doc.name, "file_url") or file_doc.file_url

    return {
        "status": "uploaded",
        "file": file_doc.name,
        "url": file_url,
        "result": result,
    }
