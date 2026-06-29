import os, re, mimetypes, hashlib
import frappe
import boto3
from botocore.config import Config

def get_password(doc, fieldname):
    try:
        return doc.get_password(fieldname)
    except Exception:
        return doc.get(fieldname)

def s3_client(bucket_doc):
    access_key = get_password(bucket_doc, "access_key")
    secret_key = get_password(bucket_doc, "secret_key")

    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=bucket_doc.region,
        endpoint_url=bucket_doc.endpoint_url or None,
        use_ssl=bool(bucket_doc.use_ssl),
        verify=bool(bucket_doc.verify_ssl),
        config=Config(signature_version="s3v4")
    )

def clean_key(value):
    value = str(value or "").strip()
    value = re.sub(r"[^\w\-./ ]+", "", value)
    value = value.replace(" ", "_")
    return value.strip("/")

def file_path(file_doc):
    if hasattr(file_doc, "get_full_path"):
        path = file_doc.get_full_path()
        if os.path.exists(path):
            return path

    file_name = os.path.basename(file_doc.file_url or file_doc.file_name or "")
    if file_doc.is_private:
        return frappe.get_site_path("private", "files", file_name)
    return frappe.get_site_path("public", "files", file_name)

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def enabled_rule_for_file(file_doc):
    if not file_doc.attached_to_doctype:
        return None

    rules = frappe.get_all(
        "S3 Vault Rule",
        filters={
            "enabled": 1,
            "reference_doctype": file_doc.attached_to_doctype
        },
        fields=["name"],
        order_by="modified desc",
        limit=1
    )

    if not rules:
        return None

    return frappe.get_doc("S3 Vault Rule", rules[0].name)

def validate_file_against_rule(file_doc, rule, path):
    size_mb = os.path.getsize(path) / (1024 * 1024)
    max_size = float(rule.max_file_size_mb or 0)

    if max_size and size_mb > max_size:
        frappe.throw(f"File size {size_mb:.2f} MB is bigger than allowed {max_size} MB")

    ext = os.path.splitext(file_doc.file_name or path)[1].replace(".", "").lower()

    blocked = [x.strip().lower() for x in (rule.blocked_extensions or "").split(",") if x.strip()]
    allowed = [x.strip().lower() for x in (rule.allowed_extensions or "").split(",") if x.strip()]

    if ext in blocked:
        frappe.throw(f"File type .{ext} is blocked")

    if allowed and ext not in allowed:
        frappe.throw(f"File type .{ext} is not allowed")

def make_object_key(file_doc, rule, bucket_doc):
    from frappe.utils import now_datetime

    dt = now_datetime()
    pattern = rule.folder_pattern or "{site}/{doctype}/{docname}/{yyyy}/{mm}"
    values = {
        "site": frappe.local.site,
        "doctype": file_doc.attached_to_doctype,
        "docname": file_doc.attached_to_name,
        "yyyy": dt.strftime("%Y"),
        "mm": dt.strftime("%m"),
        "filename": file_doc.file_name
    }

    for k, v in values.items():
        pattern = pattern.replace("{" + k + "}", clean_key(v))

    prefix = clean_key(bucket_doc.base_prefix or "")
    filename = clean_key(file_doc.file_name)
    key = "/".join(x for x in [prefix, clean_key(pattern), file_doc.name + "_" + filename] if x)
    return key

def insert_log(action, status="Success", file_doc=None, bucket_doc=None, object_key=None, error_message=None):
    try:
        log = frappe.new_doc("S3 Vault Log")
        meta_fields = {df.fieldname for df in frappe.get_meta("S3 Vault Log").fields}

        values = {
            "action": action,
            "status": status,
            "user": frappe.session.user,
            "file": file_doc.name if file_doc else None,
            "doctype_name": file_doc.attached_to_doctype if file_doc else None,
            "document_name": file_doc.attached_to_name if file_doc else None,
            "bucket": bucket_doc.name if bucket_doc else None,
            "object_key": object_key,
            "error_message": error_message
        }

        for k, v in values.items():
            if k in meta_fields:
                log.set(k, v)

        log.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Log Error")

# override previous insert_log with safer version
def insert_log(action, status="Success", file_doc=None, bucket_doc=None, object_key=None, error_message=None):
    import frappe
    try:
        log = frappe.new_doc("S3 Vault Log")
        log.name = "S3LOG-" + frappe.generate_hash(length=12)

        meta_fields = {df.fieldname for df in frappe.get_meta("S3 Vault Log").fields}

        values = {
            "action": action,
            "status": status,
            "user": frappe.session.user if getattr(frappe, "session", None) else "Administrator",
            "file": file_doc.name if file_doc else None,
            "doctype_name": file_doc.attached_to_doctype if file_doc else None,
            "document_name": file_doc.attached_to_name if file_doc else None,
            "bucket": bucket_doc.name if bucket_doc else None,
            "object_key": object_key,
            "error_message": error_message,
        }

        for k, v in values.items():
            if k in meta_fields:
                log.set(k, v)

        log.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Log Insert Failed")

# final override: Wasabi object basename must match local unique filename
def make_object_key(file_doc, rule_doc):
    import os
    import re
    import frappe
    from frappe.utils import now_datetime

    def clean_part(value):
        value = str(value or "").strip()
        value = value.replace("\\", "/")
        value = re.sub(r"[^A-Za-z0-9._/\-=]+", "_", value)
        value = re.sub(r"_+", "_", value)
        return value.strip("/_")

    now = now_datetime()

    # Use File ID as unique filename.
    # Example: Account.pdf -> 8c16ba8f7e.pdf
    ext = os.path.splitext(file_doc.file_name or "")[1]

    if not ext and file_doc.file_url:
        ext = os.path.splitext(file_doc.file_url.split("?", 1)[0])[1]

    unique_filename = f"{file_doc.name}{ext or ''}"

    folder_pattern = getattr(rule_doc, "folder_pattern", None) or "{site}/{doctype}/{docname}/{yyyy}/{mm}"

    values = {
        "site": frappe.local.site,
        "doctype": file_doc.attached_to_doctype or "File",
        "docname": file_doc.attached_to_name or file_doc.name,
        "file": file_doc.name,
        "file_name": file_doc.file_name or file_doc.name,
        "yyyy": now.strftime("%Y"),
        "mm": now.strftime("%m"),
        "dd": now.strftime("%d"),
    }

    try:
        folder = folder_pattern.format(**values)
    except Exception:
        folder = f"{frappe.local.site}/{file_doc.attached_to_doctype or 'File'}/{file_doc.attached_to_name or file_doc.name}/{now.strftime('%Y')}/{now.strftime('%m')}"

    folder = clean_part(folder)
    unique_filename = clean_part(unique_filename)

    return f"{folder}/{unique_filename}"

# final override: Wasabi object basename must match local unique filename
def make_object_key(file_doc, rule_doc):
    import os
    import re
    import frappe
    from frappe.utils import now_datetime

    def clean_part(value):
        value = str(value or "").strip()
        value = value.replace("\\", "/")
        value = re.sub(r"[^A-Za-z0-9._/\-=]+", "_", value)
        value = re.sub(r"_+", "_", value)
        return value.strip("/_")

    now = now_datetime()

    # Use File ID as unique filename.
    # Example: Account.pdf -> 8c16ba8f7e.pdf
    ext = os.path.splitext(file_doc.file_name or "")[1]

    if not ext and file_doc.file_url:
        ext = os.path.splitext(file_doc.file_url.split("?", 1)[0])[1]

    unique_filename = f"{file_doc.name}{ext or ''}"

    folder_pattern = getattr(rule_doc, "folder_pattern", None) or "{site}/{doctype}/{docname}/{yyyy}/{mm}"

    values = {
        "site": frappe.local.site,
        "doctype": file_doc.attached_to_doctype or "File",
        "docname": file_doc.attached_to_name or file_doc.name,
        "file": file_doc.name,
        "file_name": file_doc.file_name or file_doc.name,
        "yyyy": now.strftime("%Y"),
        "mm": now.strftime("%m"),
        "dd": now.strftime("%d"),
    }

    try:
        folder = folder_pattern.format(**values)
    except Exception:
        folder = f"{frappe.local.site}/{file_doc.attached_to_doctype or 'File'}/{file_doc.attached_to_name or file_doc.name}/{now.strftime('%Y')}/{now.strftime('%m')}"

    folder = clean_part(folder)
    unique_filename = clean_part(unique_filename)

    return f"{folder}/{unique_filename}"

# final compatibility override: support old calls with 2 args and existing calls with 3 args
def make_object_key(file_doc, rule_doc=None, bucket_doc=None, *args, **kwargs):
    import os
    import re
    import frappe
    from frappe.utils import now_datetime

    def clean_part(value):
        value = str(value or "").strip()
        value = value.replace("\\", "/")
        value = re.sub(r"[^A-Za-z0-9._/\-=]+", "_", value)
        value = re.sub(r"_+", "_", value)
        return value.strip("/_")

    now = now_datetime()

    # Use unique Wasabi/local filename.
    # Example: Account.pdf -> 388497874d_Account.pdf
    original_name = file_doc.file_name or file_doc.name
    ext = os.path.splitext(original_name)[1]

    if not ext and file_doc.file_url:
        ext = os.path.splitext(str(file_doc.file_url).split("?", 1)[0])[1]

    safe_original = clean_part(original_name)

    if safe_original:
        unique_filename = f"{file_doc.name}_{safe_original}"
    else:
        unique_filename = f"{file_doc.name}{ext or ''}"

    # Get folder pattern from rule_doc if available
    folder_pattern = None
    if rule_doc and hasattr(rule_doc, "folder_pattern"):
        folder_pattern = rule_doc.folder_pattern

    folder_pattern = folder_pattern or "{site}/{doctype}/{docname}/{yyyy}/{mm}"

    values = {
        "site": frappe.local.site,
        "doctype": file_doc.attached_to_doctype or "File",
        "docname": file_doc.attached_to_name or file_doc.name,
        "file": file_doc.name,
        "file_name": file_doc.file_name or file_doc.name,
        "yyyy": now.strftime("%Y"),
        "mm": now.strftime("%m"),
        "dd": now.strftime("%d"),
    }

    try:
        folder = folder_pattern.format(**values)
    except Exception:
        folder = f"{frappe.local.site}/{file_doc.attached_to_doctype or 'File'}/{file_doc.attached_to_name or file_doc.name}/{now.strftime('%Y')}/{now.strftime('%m')}"

    folder = clean_part(folder)
    unique_filename = clean_part(unique_filename)

    return f"{folder}/{unique_filename}"

# stable final override: compatible object key
def make_object_key(file_doc, rule_doc=None, bucket_doc=None, *args, **kwargs):
    import os
    import re
    import frappe
    from frappe.utils import now_datetime

    def clean_part(value):
        value = str(value or "").strip()
        value = value.replace("\\", "/")
        value = re.sub(r"[^A-Za-z0-9._/\-=]+", "_", value)
        value = re.sub(r"_+", "_", value)
        return value.strip("/_")

    now = now_datetime()

    original_name = file_doc.file_name or file_doc.name
    safe_original = clean_part(original_name)

    if safe_original:
        unique_filename = f"{file_doc.name}_{safe_original}"
    else:
        ext = os.path.splitext(original_name)[1]
        unique_filename = f"{file_doc.name}{ext or ''}"

    folder_pattern = None
    if rule_doc and hasattr(rule_doc, "folder_pattern"):
        folder_pattern = rule_doc.folder_pattern

    folder_pattern = folder_pattern or "{site}/{doctype}/{docname}/{yyyy}/{mm}"

    values = {
        "site": frappe.local.site,
        "doctype": file_doc.attached_to_doctype or "File",
        "docname": file_doc.attached_to_name or file_doc.name,
        "file": file_doc.name,
        "file_name": file_doc.file_name or file_doc.name,
        "yyyy": now.strftime("%Y"),
        "mm": now.strftime("%m"),
        "dd": now.strftime("%d"),
    }

    try:
        folder = folder_pattern.format(**values)
    except Exception:
        folder = f"{frappe.local.site}/{file_doc.attached_to_doctype or 'File'}/{file_doc.attached_to_name or file_doc.name}/{now.strftime('%Y')}/{now.strftime('%m')}"

    return f"{clean_part(folder)}/{clean_part(unique_filename)}"

# final fix: robust S3 Vault Rule matching for PDFs and mixed extension formats
def _s3vault_get_file_ext(file_doc):
    import os

    name = getattr(file_doc, "file_name", None) or getattr(file_doc, "file_url", None) or ""
    ext = os.path.splitext(str(name).split("?", 1)[0])[1].lower().strip()

    if ext.startswith("."):
        ext = ext[1:]

    return ext


def _s3vault_parse_extensions(value):
    import re

    if not value:
        return []

    parts = re.split(r"[\n,;| ]+", str(value))
    out = []

    for p in parts:
        p = p.strip().lower()
        if not p:
            continue
        if p.startswith("."):
            p = p[1:]
        out.append(p)

    return out


def _s3vault_rule_extensions(rule_doc):
    fields = [
        "allowed_extensions",
        "extensions",
        "file_extensions",
        "allowed_file_extensions",
    ]

    for field in fields:
        try:
            if rule_doc.meta.has_field(field):
                return _s3vault_parse_extensions(rule_doc.get(field))
        except Exception:
            pass

    return []


def _s3vault_rule_reference_doctype(rule_doc):
    fields = [
        "reference_doctype",
        "document_type",
        "ref_doctype",
        "doctype_name",
    ]

    for field in fields:
        try:
            if rule_doc.meta.has_field(field):
                return rule_doc.get(field)
        except Exception:
            pass

    return None


# override: find enabled rule by attached_to_doctype and allow PDF correctly
def enabled_rule_for_file(file_doc):
    import frappe

    attached_doctype = getattr(file_doc, "attached_to_doctype", None)
    if not attached_doctype:
        return None

    try:
        rule_names = frappe.get_all(
            "S3 Vault Rule",
            filters={"enabled": 1},
            pluck="name",
            order_by="modified desc",
        )
    except Exception:
        rule_names = frappe.get_all(
            "S3 Vault Rule",
            pluck="name",
            order_by="modified desc",
        )

    file_ext = _s3vault_get_file_ext(file_doc)

    for name in rule_names:
        rule = frappe.get_doc("S3 Vault Rule", name)

        ref_dt = _s3vault_rule_reference_doctype(rule)
        if ref_dt and ref_dt != attached_doctype:
            continue

        exts = _s3vault_rule_extensions(rule)

        # If rule extension list is empty, allow all extensions.
        # If not empty, file extension must be included.
        if exts and file_ext not in exts:
            continue

        return rule

    return None


# override: validate extensions in the same flexible way
def validate_file_against_rule(file_doc, rule_doc):
    import frappe

    file_ext = _s3vault_get_file_ext(file_doc)
    exts = _s3vault_rule_extensions(rule_doc)

    if exts and file_ext not in exts:
        frappe.throw(
            f"File extension .{file_ext} is not allowed by S3 Vault Rule {rule_doc.name}. Allowed: {', '.join(exts)}"
        )

    return True
