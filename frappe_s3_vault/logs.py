import re
import traceback as py_traceback

import frappe


DOCTYPE = "S3 Vault Log"
STORAGE_DOCTYPE = "S3 Vault File"


def get_log_fields():
    meta = frappe.get_meta(DOCTYPE)
    return [df.fieldname for df in meta.fields if df.fieldname]


def has_field(fieldname):
    try:
        return frappe.get_meta(DOCTYPE).has_field(fieldname)
    except Exception:
        return False


def doc_has(doc, fieldname):
    try:
        return doc.meta.has_field(fieldname)
    except Exception:
        return False


def set_if_has(doc, fieldname, value):
    if doc_has(doc, fieldname):
        doc.set(fieldname, value)


def set_if_has_not_none(doc, fieldname, value):
    if value is not None and doc_has(doc, fieldname):
        doc.set(fieldname, value)


def now():
    return frappe.utils.now()


def current_user():
    try:
        return frappe.session.user
    except Exception:
        return "Administrator"


def request_ip():
    try:
        return frappe.local.request_ip
    except Exception:
        return None


def request_user_agent():
    try:
        return frappe.get_request_header("User-Agent")
    except Exception:
        return None


def request_id():
    try:
        return frappe.local.request_id
    except Exception:
        return None


def get_file_doc(file_id):
    if file_id and frappe.db.exists("File", file_id):
        return frappe.get_doc("File", file_id)
    return None


def get_storage_file_doc(storage_file=None, file_id=None, object_key=None):
    name = None

    if storage_file and frappe.db.exists(STORAGE_DOCTYPE, storage_file):
        name = storage_file

    if not name and file_id:
        name = frappe.db.get_value(
            STORAGE_DOCTYPE,
            {"file": file_id},
            "name",
            order_by="creation desc",
        )

    if not name and object_key:
        name = frappe.db.get_value(
            STORAGE_DOCTYPE,
            {"object_key": object_key},
            "name",
            order_by="creation desc",
        )

    if name:
        return frappe.get_doc(STORAGE_DOCTYPE, name)

    return None


def get_bucket_name_from_storage(storage_doc):
    if not storage_doc:
        return None

    if storage_doc.get("bucket_name"):
        return storage_doc.get("bucket_name")

    bucket = storage_doc.get("bucket")

    if bucket and frappe.db.exists("S3 Vault Bucket", bucket):
        try:
            bucket_doc = frappe.get_doc("S3 Vault Bucket", bucket)
            return bucket_doc.get("bucket_name")
        except Exception:
            pass

    return None


def get_reference_from_file(file_doc=None, storage_doc=None):
    doctype_name = None
    document_name = None

    if file_doc:
        doctype_name = file_doc.get("attached_to_doctype")
        document_name = file_doc.get("attached_to_name")

    if storage_doc:
        doctype_name = doctype_name or storage_doc.get("attached_to_doctype")
        document_name = document_name or storage_doc.get("attached_to_name")

    return doctype_name, document_name


def normalize_status(status):
    status = str(status or "").strip()

    allowed = ["Success", "Failed", "Skipped", "Pending"]

    if status in allowed:
        return status

    if status.lower() in ["ok", "uploaded", "downloaded", "deleted"]:
        return "Success"

    if status.lower() in ["error", "fail", "failed"]:
        return "Failed"

    return status or "Success"


def normalize_action(action):
    action = str(action or "").strip()

    allowed = ["Upload", "Download", "Delete", "Repair", "Cleanup", "System"]

    if action in allowed:
        return action

    return action or "System"


def safe_text(value, max_len=10000):
    if value is None:
        return None

    value = str(value)

    if len(value) > max_len:
        return value[:max_len] + "\n...[truncated]"

    return value


def build_log_data(
    action,
    status="Success",
    file_id=None,
    storage_file=None,
    bucket_name=None,
    object_key=None,
    error_message=None,
    traceback_text=None,
    user=None,
    started_on=None,
    completed_on=None,
    duration_ms=None,
    doctype_name=None,
    document_name=None,
    ip_address=None,
    user_agent=None,
    request_id_value=None,
    link_file=True,
):
    """
    Build clean S3 Vault Log data using the real DocType fields.
    Does not insert anything.
    """

    action = normalize_action(action)
    status = normalize_status(status)

    storage_doc = get_storage_file_doc(
        storage_file=storage_file,
        file_id=file_id,
        object_key=object_key,
    )

    if storage_doc:
        storage_file = storage_doc.name
        file_id = file_id or storage_doc.get("file")
        object_key = object_key or storage_doc.get("object_key")
        bucket_name = bucket_name or get_bucket_name_from_storage(storage_doc)

    file_doc = get_file_doc(file_id)

    ref_dt, ref_name = get_reference_from_file(file_doc=file_doc, storage_doc=storage_doc)

    doctype_name = doctype_name or ref_dt
    document_name = document_name or ref_name

    # For Delete logs, avoid keeping File Link because it can block File deletion.
    if action == "Delete":
        link_file = False

    data = {
        "action": action,
        "status": status,
        "user": user or current_user(),
        "storage_file": storage_file,
        "file": file_id if link_file and file_doc else None,
        "doctype_name": doctype_name,
        "document_name": document_name,
        "bucket_name": bucket_name,
        "object_key": object_key,
        "ip_address": ip_address or request_ip(),
        "user_agent": user_agent or request_user_agent(),
        "request_id": request_id_value or request_id(),
        "started_on": started_on,
        "completed_on": completed_on or now(),
        "duration_ms": duration_ms,
        "error_message": safe_text(error_message),
        "traceback": safe_text(traceback_text),
    }

    return data


def preview_log_data(**kwargs):
    """
    Safe preview for bench execute. Does not insert.
    """

    return build_log_data(**kwargs)


def write_log(
    action,
    status="Success",
    file_id=None,
    storage_file=None,
    bucket_name=None,
    object_key=None,
    error_message=None,
    traceback_text=None,
    user=None,
    started_on=None,
    completed_on=None,
    duration_ms=None,
    doctype_name=None,
    document_name=None,
    ip_address=None,
    user_agent=None,
    request_id_value=None,
    link_file=True,
    commit=False,
):
    """
    Standard log writer.

    All upload/download/delete code should use this function.
    """

    data = build_log_data(
        action=action,
        status=status,
        file_id=file_id,
        storage_file=storage_file,
        bucket_name=bucket_name,
        object_key=object_key,
        error_message=error_message,
        traceback_text=traceback_text,
        user=user,
        started_on=started_on,
        completed_on=completed_on,
        duration_ms=duration_ms,
        doctype_name=doctype_name,
        document_name=document_name,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id_value=request_id_value,
        link_file=link_file,
    )

    doc = frappe.new_doc(DOCTYPE)

    for fieldname, value in data.items():
        set_if_has_not_none(doc, fieldname, value)

    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)

    if commit:
        frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.

    return doc.name


def write_exception_log(action, file_id=None, storage_file=None, error_message=None, commit=False):
    return write_log(
        action=action,
        status="Failed",
        file_id=file_id,
        storage_file=storage_file,
        error_message=error_message or "Exception occurred",
        traceback_text=frappe.get_traceback(),
        commit=commit,
    )


def extract_object_key_from_text(text):
    """
    Best-effort extraction for old logs.
    """

    if not text:
        return None

    text = str(text)

    # Common old format:
    # Uploaded to aogc_v16/Raven_Message/.../file.pdf; local_deleted=1
    m = re.search(r"Uploaded to\s+([^;\n\r]+)", text)
    if m:
        return m.group(1).strip()

    # Common object key style
    m = re.search(r"(aogc_v16/[^ \n\r;'\"]+)", text)
    if m:
        return m.group(1).strip()

    return None


def audit_s3_vault_log_fields():
    fields = get_log_fields()

    expected = [
        "action",
        "status",
        "user",
        "storage_file",
        "file",
        "doctype_name",
        "document_name",
        "bucket_name",
        "object_key",
        "ip_address",
        "user_agent",
        "request_id",
        "started_on",
        "completed_on",
        "duration_ms",
        "error_message",
        "traceback",
    ]

    extra_fields = []

    for field in fields:
        if field in expected:
            continue

        if field.startswith("column_break"):
            continue

        if field.endswith("_section"):
            continue

        extra_fields.append(field)

    return {
        "doctype_fields": fields,
        "recognized_fields": [f for f in expected if f in fields],
        "missing_expected_fields": [f for f in expected if f not in fields],
        "extra_fields": extra_fields,
    }


def _safe_count(sql, params=None):
    rows = frappe.db.sql(sql, params or (), as_dict=True)
    if rows:
        return list(rows[0].values())[0]
    return 0


def s3_vault_log_quality_summary(limit=30):
    fields = get_log_fields()

    summary = {
        "total_logs": frappe.db.count(DOCTYPE),
        "fields": fields,
    }

    summary["by_action_status"] = frappe.db.sql(
        """
        select action, status, count(*) as count_rows
        from `tabS3 Vault Log`
        group by action, status
        order by count_rows desc
        """,
        as_dict=True,
    )

    summary["null_file_count"] = _safe_count(
        """
        select count(*) as count_rows
        from `tabS3 Vault Log`
        where file is null or file = ''
        """
    )

    summary["invalid_file_link_count"] = _safe_count(
        """
        select count(*) as count_rows
        from `tabS3 Vault Log` l
        left join tabFile f on f.name = l.file
        where ifnull(l.file, '') != ''
          and f.name is null
        """
    )

    summary["missing_object_key_count"] = _safe_count(
        """
        select count(*) as count_rows
        from `tabS3 Vault Log`
        where object_key is null or object_key = ''
        """
    )

    summary["missing_bucket_name_count"] = _safe_count(
        """
        select count(*) as count_rows
        from `tabS3 Vault Log`
        where bucket_name is null or bucket_name = ''
        """
    )

    summary["recent_logs"] = frappe.get_all(
        DOCTYPE,
        fields=[
            "name",
            "creation",
            "action",
            "status",
            "user",
            "storage_file",
            "file",
            "doctype_name",
            "document_name",
            "bucket_name",
            "object_key",
            "error_message",
        ],
        order_by="creation desc",
        limit=int(limit),
    )

    return summary


# final override: respect S3 Vault Log Select field options
def get_select_options(fieldname):
    try:
        meta = frappe.get_meta(DOCTYPE)
        df = meta.get_field(fieldname)

        if not df or not df.options:
            return []

        return [
            x.strip()
            for x in str(df.options).split("\n")
            if x.strip()
        ]
    except Exception:
        return []


def normalize_action(action):
    action = str(action or "").strip()

    aliases = {
        "System": "Health Check",
        "Check": "Health Check",
        "Test": "Health Check",
        "URL": "Generate URL",
        "Generate": "Generate URL",
        "Exception": "Error",
        "Failed": "Error",
        "Failure": "Error",
    }

    action = aliases.get(action, action)

    allowed = get_select_options("action")

    if allowed:
        if action in allowed:
            return action

        return "Error" if "Error" in allowed else allowed[0]

    return action or "Error"


def normalize_status(status):
    status = str(status or "").strip()

    aliases = {
        "OK": "Success",
        "Ok": "Success",
        "Uploaded": "Success",
        "Downloaded": "Success",
        "Deleted": "Success",
        "Error": "Failed",
        "Fail": "Failed",
        "Failure": "Failed",
    }

    status = aliases.get(status, status)

    allowed = get_select_options("status")

    if allowed:
        if status in allowed:
            return status

        return "Failed" if "Failed" in allowed else allowed[0]

    return status or "Success"
