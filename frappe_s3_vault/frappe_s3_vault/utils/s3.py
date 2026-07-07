import mimetypes
import os
import re
import uuid
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except Exception:
    boto3 = None
    Config = None
    ClientError = Exception


def ensure_boto3():
    if boto3 is None:
        frappe.throw(_("boto3 is required. Install it with: bench pip install boto3"))


def get_settings():
    try:
        return frappe.get_single("S3 Vault Settings")
    except Exception:
        return None


def app_enabled():
    settings = get_settings()
    return bool(settings and cint(settings.enabled))


def get_password(doc, fieldname):
    try:
        return doc.get_password(fieldname)
    except Exception:
        return doc.get(fieldname)


def s3_client(bucket_doc):
    ensure_boto3()
    access_key = get_password(bucket_doc, "access_key")
    secret_key = get_password(bucket_doc, "secret_key")
    if not access_key or not secret_key:
        frappe.throw(_("Access Key and Secret Key are required for bucket {0}").format(bucket_doc.name))

    cfg = Config(
        signature_version=bucket_doc.signature_version or "s3v4",
        s3={"addressing_style": bucket_doc.addressing_style or "auto"},
    )

    return boto3.client(
        "s3",
        endpoint_url=bucket_doc.endpoint_url or None,
        region_name=bucket_doc.region or None,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        use_ssl=bool(cint(bucket_doc.use_ssl)),
        verify=bool(cint(bucket_doc.verify_ssl)),
        config=cfg,
    )


def test_bucket(bucket_doc, write_test=True):
    client = s3_client(bucket_doc)
    bucket_name = bucket_doc.bucket_name
    if not bucket_name:
        frappe.throw(_("Bucket Name is required"))

    client.head_bucket(Bucket=bucket_name)

    if write_test:
        key = build_key(bucket_doc.base_prefix, "health-check", f"{uuid.uuid4().hex}.txt")
        client.put_object(Bucket=bucket_name, Key=key, Body=b"frappe_s3_vault_health_check")
        client.head_object(Bucket=bucket_name, Key=key)
        client.delete_object(Bucket=bucket_name, Key=key)

    return True


def normalize_list(value):
    if not value:
        return []
    return [x.strip().lower().lstrip(".") for x in re.split(r"[,\n]", value) if x.strip()]


def get_file_extension(filename):
    return os.path.splitext(filename or "")[1].lower().lstrip(".")


def validate_file_against_rule(file_doc, rule):
    filename = file_doc.file_name or os.path.basename(file_doc.file_url or "")
    ext = get_file_extension(filename)

    # blocked extensions: global + rule
    settings = get_settings()
    blocked = set(normalize_list(getattr(settings, "global_blocked_extensions", "") if settings else ""))
    blocked.update(normalize_list(rule.blocked_extensions))
    if ext and ext in blocked:
        frappe.throw(_("File type .{0} is blocked by S3 Vault rule {1}").format(ext, rule.name))

    allowed = normalize_list(rule.allowed_extensions)
    if allowed and ext not in allowed:
        frappe.throw(_("File type .{0} is not allowed for {1}").format(ext, rule.reference_doctype))

    size = cint(getattr(file_doc, "file_size", 0))
    max_mb = cint(rule.max_file_size_mb)
    if max_mb and size > max_mb * 1024 * 1024:
        frappe.throw(_("File is larger than allowed size: {0} MB").format(max_mb))

    allowed_mimes = normalize_list(rule.allowed_mime_types)
    guessed_mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if allowed_mimes and guessed_mime.lower() not in allowed_mimes:
        frappe.throw(_("MIME type {0} is not allowed").format(guessed_mime))

    return guessed_mime


def find_rule_for_file(file_doc):
    if not file_doc.attached_to_doctype:
        return None

    rules = frappe.get_all(
        "S3 Vault Rule",
        filters={"enabled": 1, "reference_doctype": file_doc.attached_to_doctype},
        fields=["name", "priority"],
        order_by="priority asc, modified desc",
    )

    for r in rules:
        rule = frappe.get_doc("S3 Vault Rule", r.name)
        applies_to = rule.applies_to or "All Attachments"

        if applies_to == "Specific Attach Field":
            if not rule.attach_fieldname:
                continue
            if (file_doc.attached_to_field or "") != rule.attach_fieldname:
                continue

        elif applies_to == "File Manager Attachments":
            # Normal attachment sidebar files usually do not have attached_to_field.
            if file_doc.attached_to_field:
                continue

        return rule

    return None


def build_key(*parts):
    clean = []
    for part in parts:
        if not part:
            continue
        p = str(part).strip().replace("\\", "/")
        p = re.sub(r"/+", "/", p).strip("/")
        p = p.replace("..", "")
        if p:
            clean.append(p)
    return "/".join(clean)


def safe_filename(name):
    name = os.path.basename(name or "file")
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return name or "file"


def make_object_key(file_doc, rule, bucket_doc):
    original = safe_filename(file_doc.file_name or os.path.basename(file_doc.file_url or "file"))
    ext = get_file_extension(original)
    stem = os.path.splitext(original)[0]

    strategy = rule.filename_strategy or "Hash Prefix"
    if strategy == "UUID":
        stored = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex
    elif strategy == "Content Hash":
        h = file_doc.content_hash or file_doc.name.replace("/", "-")
        stored = f"{h}.{ext}" if ext else h
    elif strategy == "Original":
        stored = original
    else:
        stored = f"{uuid.uuid4().hex[:10]}-{original}"

    today = now_datetime()
    pattern = rule.folder_pattern or "{site}/{doctype}/{docname}/{yyyy}/{mm}"
    folder = pattern.format(
        site=frappe.local.site,
        doctype=file_doc.attached_to_doctype or "Unattached",
        docname=file_doc.attached_to_name or "Unattached",
        fieldname=file_doc.attached_to_field or "attachments",
        yyyy=today.strftime("%Y"),
        mm=today.strftime("%m"),
        dd=today.strftime("%d"),
    )

    return build_key(bucket_doc.base_prefix, folder, stored), stored


def get_local_file_path(file_doc):
    file_url = file_doc.file_url or ""
    if file_url.startswith("/api/method/frappe_s3_vault"):
        return None
    if file_url.startswith("/private/files/") or file_url.startswith("/files/"):
        return file_doc.get_full_path()
    try:
        return file_doc.get_full_path()
    except Exception:
        return None


def create_log(action, status="Success", **kwargs):
    try:
        doc = frappe.new_doc("S3 Vault Log")
        doc.action = action
        doc.status = status
        doc.user = frappe.session.user
        doc.started_on = kwargs.pop("started_on", now_datetime())
        doc.completed_on = now_datetime()
        for k, v in kwargs.items():
            if hasattr(doc, k):
                setattr(doc, k, v)
        doc.insert(ignore_permissions=True)
        frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Log Failed")


def get_storage_file_by_file(file_name):
    name = frappe.db.get_value("S3 Vault File", {"file": file_name}, "name")
    return frappe.get_doc("S3 Vault File", name) if name else None


def assert_document_permission(storage_file, ptype="read"):
    if not storage_file.attached_to_doctype or not storage_file.attached_to_name:
        frappe.throw(_("File is not attached to a document"), frappe.PermissionError)
    if not frappe.has_permission(storage_file.attached_to_doctype, ptype, storage_file.attached_to_name):
        frappe.throw(_("Not permitted to access this attachment"), frappe.PermissionError)
