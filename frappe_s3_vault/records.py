import os
import mimetypes
from urllib.parse import unquote

import frappe


DOWNLOAD_PREFIX = "/api/method/frappe_s3_vault.api.download"

STATUS_PENDING = "Pending"
STATUS_UPLOADED = "Uploaded"
STATUS_FAILED = "Failed"
STATUS_SOFT_DELETED = "Soft Deleted"
STATUS_DELETED = "Deleted"
STATUS_MISSING = "Missing"


def secure_url(file_id):
    return f"{DOWNLOAD_PREFIX}?file={file_id}"


def has_field(doctype, fieldname):
    try:
        return frappe.get_meta(doctype).has_field(fieldname)
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


def get_file_doc(file_id):
    if file_id and frappe.db.exists("File", file_id):
        return frappe.get_doc("File", file_id)
    return None


def normalize_local_url(file_url):
    if not file_url:
        return None

    file_url = unquote(str(file_url))

    if file_url.startswith("/private/files/") or file_url.startswith("/files/"):
        return file_url

    return None


def get_file_extension(file_name=None, file_url=None, object_key=None):
    source = file_name or file_url or object_key or ""
    ext = os.path.splitext(str(source).split("?", 1)[0])[1].lower().strip()

    if ext.startswith("."):
        ext = ext[1:]

    return ext or None


def get_content_type(file_name=None, local_path=None, object_key=None):
    source = file_name or local_path or object_key
    if not source:
        return None

    content_type, _encoding = mimetypes.guess_type(source)
    return content_type


def get_file_size(local_path=None):
    if local_path and os.path.isfile(local_path):
        return os.path.getsize(local_path)
    return None


def get_stored_file_name(object_key=None, file_id=None, file_name=None):
    if object_key:
        return os.path.basename(object_key)

    if file_id and file_name:
        return f"{file_id}_{file_name}"

    return file_name


def get_bucket_doc(bucket):
    if not bucket:
        return None

    if isinstance(bucket, str):
        if frappe.db.exists("S3 Vault Bucket", bucket):
            return frappe.get_doc("S3 Vault Bucket", bucket)
        return None

    return bucket


def get_rule_doc(rule):
    if not rule:
        return None

    if isinstance(rule, str):
        if frappe.db.exists("S3 Vault Rule", rule):
            return frappe.get_doc("S3 Vault Rule", rule)
        return None

    return rule


def get_existing_vault_name(file_id, include_deleted=True):
    if not file_id:
        return None

    filters = {"file": file_id}

    if not include_deleted:
        filters["status"] = ["!=", STATUS_DELETED]

    return frappe.db.get_value(
        "S3 Vault File",
        filters,
        "name",
        order_by="creation desc",
    )


def get_vault_doc(file_id, create=True):
    existing = get_existing_vault_name(file_id)

    if existing:
        return frappe.get_doc("S3 Vault File", existing)

    if not create:
        return None

    doc = frappe.new_doc("S3 Vault File")
    set_if_has(doc, "file", file_id)
    return doc


def save_doc(doc):
    doc.flags.ignore_permissions = True

    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)

    return doc


def fill_file_reference_fields(doc, file_doc):
    if not file_doc:
        return

    set_if_has(doc, "file", file_doc.name)
    set_if_has(doc, "file_url", secure_url(file_doc.name))

    set_if_has(doc, "attached_to_doctype", file_doc.attached_to_doctype)
    set_if_has(doc, "attached_to_name", file_doc.attached_to_name)
    set_if_has(doc, "attached_to_field", file_doc.attached_to_field)

    set_if_has(doc, "original_file_name", file_doc.file_name)
    set_if_has(doc, "is_private", file_doc.is_private)


def upsert_uploaded_file(
    file_id,
    rule=None,
    bucket=None,
    object_key=None,
    local_file_url=None,
    local_path=None,
    file_hash=None,
    etag=None,
    version_id=None,
    content_type=None,
    file_size=None,
    extra=None,
):
    """
    Create/update S3 Vault File after successful Wasabi upload.
    This should become the only function used by upload_file_to_s3()
    for S3 Vault File writes.
    """

    file_doc = get_file_doc(file_id)
    if not file_doc:
        frappe.throw(f"File not found: {file_id}")

    rule_doc = get_rule_doc(rule)
    bucket_doc = get_bucket_doc(bucket)

    doc = get_vault_doc(file_id, create=True)

    if not local_file_url:
        local_file_url = normalize_local_url(file_doc.file_url)

    if not content_type:
        content_type = get_content_type(file_doc.file_name, local_path, object_key)

    if file_size is None:
        file_size = get_file_size(local_path)

    stored_file_name = get_stored_file_name(object_key, file_id, file_doc.file_name)
    file_extension = get_file_extension(file_doc.file_name, file_doc.file_url, object_key)

    fill_file_reference_fields(doc, file_doc)

    # Rule / bucket
    set_if_has_not_none(doc, "rule_used", rule_doc.name if rule_doc else rule)
    set_if_has_not_none(doc, "bucket", bucket_doc.name if bucket_doc else bucket)

    bucket_name = None
    if bucket_doc:
        bucket_name = getattr(bucket_doc, "bucket_name", None)

    set_if_has_not_none(doc, "bucket_name", bucket_name)

    # Storage identity
    set_if_has_not_none(doc, "object_key", object_key)
    set_if_has_not_none(doc, "stored_file_name", stored_file_name)

    # File info
    set_if_has_not_none(doc, "file_size", file_size)
    set_if_has_not_none(doc, "content_type", content_type)
    set_if_has_not_none(doc, "file_extension", file_extension)
    set_if_has_not_none(doc, "file_hash", file_hash)
    set_if_has_not_none(doc, "etag", etag)
    set_if_has_not_none(doc, "version_id", version_id)

    # URLs
    set_if_has(doc, "file_url", secure_url(file_id))
    set_if_has_not_none(doc, "local_file_url", local_file_url)

    # Status
    set_if_has(doc, "status", STATUS_UPLOADED)
    set_if_has(doc, "local_file_deleted", 0)
    set_if_has(doc, "deleted_from_storage", 0)
    set_if_has(doc, "error_message", None)

    # Upload audit
    set_if_has(doc, "uploaded_on", frappe.utils.now())
    set_if_has(doc, "uploaded_by", frappe.session.user)

    # Clear delete fields on successful re-upload
    set_if_has(doc, "deleted_on", None)
    set_if_has(doc, "deleted_by", None)

    if isinstance(extra, dict):
        for key, value in extra.items():
            set_if_has_not_none(doc, key, value)

    save_doc(doc)
    return doc


def mark_local_deleted(file_id, deleted=True):
    doc = get_vault_doc(file_id, create=False)

    if not doc:
        return None

    set_if_has(doc, "local_file_deleted", 1 if deleted else 0)

    save_doc(doc)
    return doc


def mark_upload_failed(file_id, message=None):
    doc = get_vault_doc(file_id, create=True)

    file_doc = get_file_doc(file_id)
    if file_doc:
        fill_file_reference_fields(doc, file_doc)

    set_if_has(doc, "status", STATUS_FAILED)
    set_if_has(doc, "error_message", message)

    save_doc(doc)
    return doc


def mark_missing(file_id, message=None):
    doc = get_vault_doc(file_id, create=False)

    if not doc:
        return None

    set_if_has(doc, "status", STATUS_MISSING)
    set_if_has(doc, "error_message", message)

    save_doc(doc)
    return doc


def mark_accessed(file_id):
    doc = get_vault_doc(file_id, create=False)

    if not doc:
        return None

    set_if_has(doc, "last_accessed_on", frappe.utils.now())

    save_doc(doc)
    return doc


def mark_deleted_from_storage(file_id, release_file_link=False):
    doc = get_vault_doc(file_id, create=False)

    if not doc:
        return None

    set_if_has(doc, "status", STATUS_DELETED)
    set_if_has(doc, "deleted_from_storage", 1)
    set_if_has(doc, "deleted_on", frappe.utils.now())
    set_if_has(doc, "deleted_by", frappe.session.user)

    save_doc(doc)

    if release_file_link:
        release_file_link_from_vault(file_id)

    return doc


def release_file_link_from_vault(file_id):
    """
    Release File Link only when File deletion is blocked.
    Keep S3 Vault File row for audit/history.
    """

    if not has_field("S3 Vault File", "file"):
        return

    try:
        frappe.db.sql(
            "update `tabS3 Vault File` set file=NULL where file=%s",
            file_id,
        )
        frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.
    except Exception:
        frappe.db.sql(
            "update `tabS3 Vault File` set file='' where file=%s",
            file_id,
        )
        frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.


def get_uploaded_record(file_id):
    name = frappe.db.get_value(
        "S3 Vault File",
        {
            "file": file_id,
            "status": STATUS_UPLOADED,
        },
        "name",
        order_by="creation desc",
    )

    if not name:
        return None

    return frappe.get_doc("S3 Vault File", name)


def get_uploaded_record_data(file_id):
    doc = get_uploaded_record(file_id)

    if not doc:
        return None

    return doc.as_dict()


def audit_s3_vault_file_fields():
    meta = frappe.get_meta("S3 Vault File")
    fields = [df.fieldname for df in meta.fields if df.fieldname]

    expected = [
        "file",
        "file_url",
        "attached_to_doctype",
        "attached_to_name",
        "attached_to_field",
        "rule_used",
        "bucket",
        "bucket_name",
        "object_key",
        "original_file_name",
        "stored_file_name",
        "file_size",
        "content_type",
        "file_extension",
        "file_hash",
        "etag",
        "version_id",
        "is_private",
        "status",
        "uploaded_on",
        "uploaded_by",
        "last_accessed_on",
        "error_message",
        "local_file_url",
        "local_file_deleted",
        "deleted_on",
        "deleted_by",
        "deleted_from_storage",
    ]

    return {
        "doctype_fields": fields,
        "recognized_fields": [f for f in expected if f in fields],
        "missing_expected_fields": [f for f in expected if f not in fields],
        "extra_fields": [f for f in fields if f not in expected and not f.startswith("column_break") and not f.endswith("_section")],
    }


def refresh_vault_file_metadata(file_id=None, vault_name=None):
    """
    Safely backfill missing S3 Vault File metadata.

    This does NOT:
    - upload to Wasabi
    - delete local files
    - change tabFile.file_url
    - change Raven messages

    It only fills missing/empty S3 Vault File columns.
    """

    if not vault_name:
        if not file_id:
            frappe.throw("file_id or vault_name is required")

        vault_name = get_existing_vault_name(file_id)

    if not vault_name or not frappe.db.exists("S3 Vault File", vault_name):
        return f"S3 Vault File record not found for file_id={file_id}"

    doc = frappe.get_doc("S3 Vault File", vault_name)

    if not file_id:
        file_id = doc.get("file")

    file_doc = get_file_doc(file_id)

    # Fill from File DocType
    if file_doc:
        fill_file_reference_fields(doc, file_doc)

        if not doc.get("original_file_name"):
            set_if_has_not_none(doc, "original_file_name", file_doc.file_name)

        if not doc.get("is_private"):
            set_if_has(doc, "is_private", file_doc.is_private)

        if not doc.get("uploaded_by"):
            set_if_has_not_none(doc, "uploaded_by", file_doc.owner)

        if not doc.get("local_file_url"):
            set_if_has_not_none(doc, "local_file_url", normalize_local_url(file_doc.file_url))

        # File size from File DocType if available
        file_size = file_doc.get("file_size") if hasattr(file_doc, "get") else None
        if file_size and not doc.get("file_size"):
            set_if_has_not_none(doc, "file_size", file_size)

    # Fill from object_key
    object_key = doc.get("object_key")

    if object_key:
        if not doc.get("stored_file_name"):
            set_if_has_not_none(doc, "stored_file_name", os.path.basename(object_key))

        if not doc.get("file_extension"):
            set_if_has_not_none(
                doc,
                "file_extension",
                get_file_extension(
                    file_doc.file_name if file_doc else None,
                    None,
                    object_key,
                ),
            )

        if not doc.get("content_type"):
            set_if_has_not_none(
                doc,
                "content_type",
                get_content_type(
                    file_doc.file_name if file_doc else None,
                    None,
                    object_key,
                ),
            )

    # Fill bucket_name from S3 Vault Bucket
    bucket_doc = get_bucket_doc(doc.get("bucket"))

    if bucket_doc:
        if not doc.get("bucket_name"):
            set_if_has_not_none(doc, "bucket_name", getattr(bucket_doc, "bucket_name", None))

    # Fill rule_used from current matching rule if missing
    if file_doc and not doc.get("rule_used"):
        try:
            from frappe_s3_vault.utils import enabled_rule_for_file
            rule_doc = enabled_rule_for_file(file_doc)

            if rule_doc:
                set_if_has_not_none(doc, "rule_used", rule_doc.name)
        except Exception:
            pass

    # Fill uploaded_on if missing
    if not doc.get("uploaded_on"):
        set_if_has_not_none(doc, "uploaded_on", doc.creation)

    # If object_key exists and status is empty, mark Uploaded
    if object_key and not doc.get("status"):
        set_if_has(doc, "status", STATUS_UPLOADED)

    save_doc(doc)

    return doc.as_dict()


def refresh_all_vault_file_metadata(limit=500):
    rows = frappe.get_all(
        "S3 Vault File",
        fields=["name", "file"],
        order_by="creation desc",
        limit=limit,
    )

    updated = []

    for row in rows:
        try:
            updated.append(refresh_vault_file_metadata(file_id=row.file, vault_name=row.name).get("name"))
        except Exception:
            frappe.log_error(frappe.get_traceback(), "S3 Vault File Metadata Refresh Failed")

    return {
        "updated_count": len(updated),
        "updated_records": updated,
    }


def _is_empty(value):
    return value is None or value == "" or value == 0


def _merge_value(current, incoming):
    if _is_empty(current) and not _is_empty(incoming):
        return incoming
    return current


def duplicate_vault_file_summary():
    rows = frappe.db.sql(
        """
        select file, count(*) as count_rows
        from `tabS3 Vault File`
        where ifnull(file, '') != ''
        group by file
        having count(*) > 1
        order by count_rows desc
        """,
        as_dict=True,
    )

    return rows


def merge_duplicate_vault_file_records(file_id, dry_run=1):
    """
    Merge duplicate S3 Vault File records for the same File.

    Keeps one best row and deletes duplicate rows.
    Does NOT touch Wasabi, local files, Raven messages, or tabFile.
    """

    dry_run = int(dry_run or 0)

    rows = frappe.get_all(
        "S3 Vault File",
        filters={"file": file_id},
        fields=[
            "name",
            "creation",
            "modified",
            "file",
            "file_url",
            "attached_to_doctype",
            "attached_to_name",
            "attached_to_field",
            "rule_used",
            "bucket",
            "bucket_name",
            "object_key",
            "original_file_name",
            "stored_file_name",
            "file_size",
            "content_type",
            "file_extension",
            "file_hash",
            "etag",
            "version_id",
            "is_private",
            "status",
            "uploaded_on",
            "uploaded_by",
            "last_accessed_on",
            "error_message",
            "local_file_url",
            "local_file_deleted",
            "deleted_on",
            "deleted_by",
            "deleted_from_storage",
        ],
        order_by="creation desc",
    )

    if len(rows) <= 1:
        return {
            "file": file_id,
            "message": "No duplicates",
            "count": len(rows),
        }

    def score(row):
        s = 0

        if row.status == STATUS_UPLOADED:
            s += 100

        if row.object_key:
            s += 50

        if row.file_url:
            s += 20

        if row.bucket:
            s += 10

        if row.local_file_deleted:
            s += 5

        return s

    rows = sorted(rows, key=score, reverse=True)

    keep_row = rows[0]
    duplicate_rows = rows[1:]

    keep = frappe.get_doc("S3 Vault File", keep_row.name)

    # Merge useful values from duplicates into keep row
    fieldnames = [
        "file",
        "file_url",
        "attached_to_doctype",
        "attached_to_name",
        "attached_to_field",
        "rule_used",
        "bucket",
        "bucket_name",
        "object_key",
        "original_file_name",
        "stored_file_name",
        "file_size",
        "content_type",
        "file_extension",
        "file_hash",
        "etag",
        "version_id",
        "is_private",
        "uploaded_on",
        "uploaded_by",
        "last_accessed_on",
        "error_message",
        "local_file_url",
        "deleted_on",
        "deleted_by",
    ]

    for row in duplicate_rows:
        for fieldname in fieldnames:
            if doc_has(keep, fieldname):
                current = keep.get(fieldname)
                incoming = row.get(fieldname)
                keep.set(fieldname, _merge_value(current, incoming))

    # Status merge
    statuses = [r.status for r in rows if r.status]

    if STATUS_UPLOADED in statuses:
        set_if_has(keep, "status", STATUS_UPLOADED)
    elif STATUS_DELETED in statuses:
        set_if_has(keep, "status", STATUS_DELETED)
    elif STATUS_FAILED in statuses:
        set_if_has(keep, "status", STATUS_FAILED)
    elif STATUS_MISSING in statuses:
        set_if_has(keep, "status", STATUS_MISSING)

    # Boolean merge
    any_local_deleted = any(int(r.local_file_deleted or 0) == 1 for r in rows)
    any_deleted_storage = any(int(r.deleted_from_storage or 0) == 1 for r in rows)

    set_if_has(keep, "local_file_deleted", 1 if any_local_deleted else 0)
    set_if_has(keep, "deleted_from_storage", 1 if any_deleted_storage else 0)

    result = {
        "file": file_id,
        "keep": keep.name,
        "delete_duplicates": [r.name for r in duplicate_rows],
        "dry_run": dry_run,
    }

    if dry_run:
        return result

    save_doc(keep)

    for row in duplicate_rows:
        try:
            frappe.delete_doc(
                "S3 Vault File",
                row.name,
                force=True,
                ignore_permissions=True,
            )
        except Exception:
            frappe.db.sql(
                "delete from `tabS3 Vault File` where name=%s",
                row.name,
            )

    frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.

    return result


def deduplicate_all_vault_files(dry_run=1, limit=500):
    dry_run = int(dry_run or 0)

    rows = frappe.db.sql(
        """
        select file, count(*) as count_rows
        from `tabS3 Vault File`
        where ifnull(file, '') != ''
        group by file
        having count(*) > 1
        order by count_rows desc
        limit %s
        """,
        int(limit),
        as_dict=True,
    )

    out = []

    for row in rows:
        out.append(merge_duplicate_vault_file_records(row.file, dry_run=dry_run))

    return {
        "dry_run": dry_run,
        "duplicate_files": len(rows),
        "results": out,
    }


def api_url_without_vault_summary(limit=100):
    """
    Find File rows that have S3 API URL but no linked S3 Vault File row.
    """

    rows = frappe.db.sql(
        """
        select
            f.name,
            f.file_name,
            f.file_url,
            f.attached_to_doctype,
            f.attached_to_name,
            f.attached_to_field
        from (
            select
                name,
                file_name,
                file_url,
                attached_to_doctype,
                attached_to_name,
                attached_to_field,
                creation
            from tabFile
            where file_url like %s
            order by creation desc
            limit %s
        ) f
        left join `tabS3 Vault File` vf
            on vf.file = f.name
        where vf.name is null
        order by f.creation desc
        """,
        (DOWNLOAD_PREFIX + "%", int(limit)),
        as_dict=True,
    )

    return rows


def find_released_vault_candidates(file_id, limit=5):
    """
    Find old S3 Vault File rows where file link was released,
    but object_key still contains this File ID.
    """

    return frappe.db.sql(
        """
        select
            name,
            file,
            bucket,
            bucket_name,
            object_key,
            status,
            deleted_from_storage,
            local_file_deleted,
            creation
        from `tabS3 Vault File`
        where
            (file is null or file = '')
            and object_key like %s
        order by
            case
                when status = 'Uploaded' then 1
                when status = 'Deleted' then 2
                when status = 'Missing' then 3
                else 9
            end,
            creation desc
        limit %s
        """,
        (f"%{file_id}%", int(limit)),
        as_dict=True,
    )


def relink_released_vault_file(file_id, dry_run=1):
    """
    Relink an old released S3 Vault File row back to tabFile.

    Safe behavior:
    - does not upload
    - does not delete
    - does not change object_key
    - keeps Deleted status if the object was deleted
    """

    dry_run = int(dry_run or 0)

    if not frappe.db.exists("File", file_id):
        return {
            "file": file_id,
            "status": "skipped",
            "reason": "File row does not exist",
        }

    existing = frappe.db.get_value(
        "S3 Vault File",
        {"file": file_id},
        "name",
        order_by="creation desc",
    )

    if existing:
        return {
            "file": file_id,
            "status": "skipped",
            "reason": "Already has linked S3 Vault File",
            "vault_file": existing,
        }

    candidates = find_released_vault_candidates(file_id, limit=5)

    if not candidates:
        return {
            "file": file_id,
            "status": "not_found",
            "reason": "No released S3 Vault File candidate found by object_key",
        }

    chosen = candidates[0]

    result = {
        "file": file_id,
        "status": "dry_run" if dry_run else "relinked",
        "chosen_vault_file": chosen.name,
        "chosen_status": chosen.status,
        "object_key": chosen.object_key,
        "all_candidates": [c.name for c in candidates],
    }

    if dry_run:
        return result

    frappe.db.sql(
        "update `tabS3 Vault File` set file=%s where name=%s",
        (file_id, chosen.name),
    )

    frappe.db.set_value(
        "File",
        file_id,
        "file_url",
        secure_url(file_id),
        update_modified=False,
    )

    frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.

    try:
        refreshed = refresh_vault_file_metadata(file_id=file_id, vault_name=chosen.name)
        result["refreshed"] = refreshed.get("name")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Relink Metadata Refresh Failed")

    return result


def repair_api_url_without_vault(limit=100, dry_run=1):
    """
    Repair File rows that have API URL but no linked S3 Vault File row.
    """

    rows = api_url_without_vault_summary(limit=limit)
    out = []

    for row in rows:
        out.append(relink_released_vault_file(row.name, dry_run=dry_run))

    return {
        "dry_run": int(dry_run or 0),
        "found": len(rows),
        "results": out,
    }


def _candidate_local_urls_for_file_name(file_name):
    if not file_name:
        return []

    return [
        f"/private/files/{file_name}",
        f"/files/{file_name}",
    ]


def _site_path_for_file_url(file_url):
    from urllib.parse import unquote
    import os

    file_url = unquote(str(file_url or ""))

    if file_url.startswith("/private/files/"):
        return frappe.get_site_path("private", "files", os.path.basename(file_url))

    if file_url.startswith("/files/"):
        return frappe.get_site_path("public", "files", os.path.basename(file_url))

    return None


def find_existing_local_file_url(file_name):
    """
    Find existing physical local file URL by exact file_name.
    """

    import os

    for file_url in _candidate_local_urls_for_file_name(file_name):
        path = _site_path_for_file_url(file_url)

        if path and os.path.isfile(path):
            return {
                "file_url": file_url,
                "path": path,
            }

    return None


def repair_api_url_without_vault_from_local(file_id, dry_run=1):
    """
    Repair File rows that have API URL but no S3 Vault File by re-uploading
    from the local physical file if it still exists.

    Safe:
    - dry_run=1 does not change anything
    - if upload fails, original API URL is restored
    """

    dry_run = int(dry_run or 0)

    if not frappe.db.exists("File", file_id):
        return {
            "file": file_id,
            "status": "skipped",
            "reason": "File row does not exist",
        }

    existing = frappe.db.get_value(
        "S3 Vault File",
        {"file": file_id},
        "name",
        order_by="creation desc",
    )

    if existing:
        return {
            "file": file_id,
            "status": "skipped",
            "reason": "Already has S3 Vault File",
            "vault_file": existing,
        }

    file_doc = frappe.get_doc("File", file_id)
    old_url = file_doc.file_url

    found = find_existing_local_file_url(file_doc.file_name)

    if not found:
        return {
            "file": file_id,
            "file_name": file_doc.file_name,
            "status": "not_repairable",
            "reason": "No local physical file found and no S3 Vault File candidate exists",
        }

    result = {
        "file": file_id,
        "file_name": file_doc.file_name,
        "status": "dry_run" if dry_run else "reuploaded",
        "local_file_url": found["file_url"],
        "local_path": found["path"],
        "old_url": old_url,
    }

    if dry_run:
        return result

    try:
        # Restore local URL only long enough for upload.py to read the file.
        frappe.db.set_value(
            "File",
            file_id,
            "file_url",
            found["file_url"],
            update_modified=False,
        )
        frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.

        from frappe_s3_vault.upload import upload_file_to_s3

        upload_result = upload_file_to_s3(file_id)
        result["upload_result"] = upload_result

        return result

    except Exception:
        # Restore old API URL if repair fails.
        frappe.db.set_value(
            "File",
            file_id,
            "file_url",
            old_url,
            update_modified=False,
        )
        frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.

        raise


def repair_all_api_url_without_vault_from_local(limit=100, dry_run=1):
    rows = api_url_without_vault_summary(limit=limit)

    out = []

    for row in rows:
        out.append(
            repair_api_url_without_vault_from_local(
                file_id=row.name,
                dry_run=dry_run,
            )
        )

    return {
        "dry_run": int(dry_run or 0),
        "found": len(rows),
        "results": out,
    }


def mark_unrecoverable_api_file_missing(file_id, reason=None):
    """
    Create a clean S3 Vault File record for an API File row that has:
    - no linked S3 Vault File
    - no local physical file
    - no released S3 Vault File candidate

    This does NOT upload, delete, or touch Wasabi.
    It only makes the S3 Vault File audit data clean.
    """

    if not frappe.db.exists("File", file_id):
        return {
            "file": file_id,
            "status": "skipped",
            "reason": "File row does not exist",
        }

    existing = frappe.db.get_value(
        "S3 Vault File",
        {"file": file_id},
        "name",
        order_by="creation desc",
    )

    if existing:
        return {
            "file": file_id,
            "status": "skipped",
            "reason": "Already has S3 Vault File",
            "vault_file": existing,
        }

    file_doc = frappe.get_doc("File", file_id)
    doc = get_vault_doc(file_id, create=True)

    fill_file_reference_fields(doc, file_doc)

    # Keep API URL
    set_if_has(doc, "file_url", secure_url(file_id))

    # Fill known file metadata
    set_if_has_not_none(doc, "original_file_name", file_doc.file_name)
    set_if_has_not_none(doc, "content_type", get_content_type(file_doc.file_name))
    set_if_has_not_none(doc, "file_extension", get_file_extension(file_doc.file_name, file_doc.file_url))
    set_if_has_not_none(doc, "is_private", file_doc.is_private)

    file_size = file_doc.get("file_size") if hasattr(file_doc, "get") else None
    set_if_has_not_none(doc, "file_size", file_size)

    # Try to fill matching rule/bucket for audit only
    try:
        from frappe_s3_vault.utils import enabled_rule_for_file
        rule_doc = enabled_rule_for_file(file_doc)

        if rule_doc:
            set_if_has_not_none(doc, "rule_used", rule_doc.name)
            set_if_has_not_none(doc, "bucket", rule_doc.bucket)

            try:
                bucket_doc = frappe.get_doc("S3 Vault Bucket", rule_doc.bucket)
                set_if_has_not_none(doc, "bucket_name", getattr(bucket_doc, "bucket_name", None))
            except Exception:
                pass
    except Exception:
        pass

    # There is no object_key because no S3 record was found
    set_if_has(doc, "object_key", None)
    set_if_has(doc, "stored_file_name", None)

    # Missing status
    set_if_has(doc, "status", STATUS_MISSING)
    set_if_has(doc, "local_file_deleted", 1)
    set_if_has(doc, "deleted_from_storage", 0)

    set_if_has(
        doc,
        "error_message",
        reason or "Unrecoverable file: API URL exists, but no local file and no S3 Vault File record/candidate was found.",
    )

    save_doc(doc)

    return {
        "file": file_id,
        "status": "marked_missing",
        "vault_file": doc.name,
    }


def mark_all_unrecoverable_api_files_missing(limit=100, dry_run=1):
    """
    Mark remaining API URL rows without S3 Vault File as Missing.
    """

    dry_run = int(dry_run or 0)
    rows = api_url_without_vault_summary(limit=limit)

    out = []

    for row in rows:
        result = {
            "file": row.name,
            "file_name": row.file_name,
            "dry_run": dry_run,
        }

        if dry_run:
            result["action"] = "would_mark_missing"
        else:
            result.update(
                mark_unrecoverable_api_file_missing(
                    row.name,
                    reason="Unrecoverable: no local physical file and no released S3 Vault File candidate exists.",
                )
            )

        out.append(result)

    return {
        "dry_run": dry_run,
        "found": len(rows),
        "results": out,
    }


# final override: object_key is mandatory, so Missing records need a safe placeholder key
def _safe_missing_object_key(file_doc):
    import os
    import re

    site = getattr(frappe.local, "site", "site")
    file_id = file_doc.name
    file_name = file_doc.file_name or file_doc.name

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name).strip("_")
    safe_name = safe_name or file_id

    return f"__missing__/{site}/{file_id}/{safe_name}"


def mark_unrecoverable_api_file_missing(file_id, reason=None):
    """
    Create a clean S3 Vault File record for an unrecoverable API file.

    Used when:
    - File has API URL
    - no linked S3 Vault File exists
    - no local physical file exists
    - no released S3 Vault File candidate exists

    object_key is mandatory in S3 Vault File, so we store a safe placeholder key.
    """

    if not frappe.db.exists("File", file_id):
        return {
            "file": file_id,
            "status": "skipped",
            "reason": "File row does not exist",
        }

    existing = frappe.db.get_value(
        "S3 Vault File",
        {"file": file_id},
        "name",
        order_by="creation desc",
    )

    if existing:
        return {
            "file": file_id,
            "status": "skipped",
            "reason": "Already has S3 Vault File",
            "vault_file": existing,
        }

    file_doc = frappe.get_doc("File", file_id)
    doc = get_vault_doc(file_id, create=True)

    fill_file_reference_fields(doc, file_doc)

    missing_key = _safe_missing_object_key(file_doc)

    # Required/core
    set_if_has(doc, "file", file_id)
    set_if_has(doc, "file_url", secure_url(file_id))
    set_if_has(doc, "object_key", missing_key)
    set_if_has(doc, "stored_file_name", file_doc.file_name or file_id)

    # File metadata
    set_if_has_not_none(doc, "original_file_name", file_doc.file_name)
    set_if_has_not_none(doc, "content_type", get_content_type(file_doc.file_name))
    set_if_has_not_none(doc, "file_extension", get_file_extension(file_doc.file_name, file_doc.file_url))
    set_if_has_not_none(doc, "is_private", file_doc.is_private)

    file_size = file_doc.get("file_size") if hasattr(file_doc, "get") else None
    set_if_has_not_none(doc, "file_size", file_size)

    # Try to fill rule/bucket for audit
    try:
        from frappe_s3_vault.utils import enabled_rule_for_file

        rule_doc = enabled_rule_for_file(file_doc)

        if rule_doc:
            set_if_has_not_none(doc, "rule_used", rule_doc.name)
            set_if_has_not_none(doc, "bucket", rule_doc.bucket)

            try:
                bucket_doc = frappe.get_doc("S3 Vault Bucket", rule_doc.bucket)
                set_if_has_not_none(doc, "bucket_name", getattr(bucket_doc, "bucket_name", None))
            except Exception:
                pass
    except Exception:
        pass

    # Missing status
    set_if_has(doc, "status", STATUS_MISSING)
    set_if_has(doc, "local_file_deleted", 1)
    set_if_has(doc, "deleted_from_storage", 0)
    set_if_has(doc, "uploaded_on", frappe.utils.now())
    set_if_has(doc, "uploaded_by", frappe.session.user)

    set_if_has(
        doc,
        "error_message",
        reason or "Unrecoverable file: API URL exists, but no local file and no S3 Vault File record/candidate was found.",
    )

    save_doc(doc)

    return {
        "file": file_id,
        "status": "marked_missing",
        "vault_file": doc.name,
        "object_key": missing_key,
    }


def mark_all_unrecoverable_api_files_missing(limit=100, dry_run=1):
    """
    Mark remaining API URL rows without S3 Vault File as Missing.
    """

    dry_run = int(dry_run or 0)
    rows = api_url_without_vault_summary(limit=limit)

    out = []

    for row in rows:
        result = {
            "file": row.name,
            "file_name": row.file_name,
            "dry_run": dry_run,
        }

        if dry_run:
            result["action"] = "would_mark_missing"
            result["object_key"] = f"__missing__/{getattr(frappe.local, 'site', 'site')}/{row.name}/{row.file_name}"
        else:
            result.update(
                mark_unrecoverable_api_file_missing(
                    row.name,
                    reason="Unrecoverable: no local physical file and no released S3 Vault File candidate exists.",
                )
            )

        out.append(result)

    return {
        "dry_run": dry_run,
        "found": len(rows),
        "results": out,
    }
