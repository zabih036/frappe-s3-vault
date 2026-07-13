from __future__ import annotations

import json
import mimetypes
import os
import re
from collections.abc import Iterable
from datetime import datetime

import frappe
from botocore.exceptions import ClientError
from frappe import _
from frappe.utils import cint

from frappe_s3_vault.logs import write_log
from frappe_s3_vault.utils import s3_client

MAX_DIRECT_UPLOAD_BYTES = 500 * 1024 * 1024
MAX_LIST_PAGE_SIZE = 1000
DEFAULT_LIST_PAGE_SIZE = 100
DEFAULT_URL_EXPIRY_SECONDS = 900
MAX_BULK_ITEMS = 200
MAX_FOLDER_SUMMARY_OBJECTS = 10_000
MAX_BACKGROUND_OBJECTS = 100_000
MAX_ZIP_OBJECTS = 5_000
MAX_ZIP_UNCOMPRESSED_BYTES = 20 * 1024 * 1024 * 1024
TEMP_ARCHIVE_RETENTION_HOURS = 24

TERMINAL_OPERATION_STATUSES = {
    "Completed",
    "Partially Completed",
    "Failed",
    "Cancelled",
}
RUNNING_OPERATION_STATUSES = {"Queued", "Running"}


def require_system_manager() -> None:
    # Kept for Phase 1/2 compatibility. Phase 3 allows the dedicated S3 Vault
    # roles and enforces connection/prefix permissions in each API.
    from frappe_s3_vault.file_manager_permissions import require_page_access

    require_page_access()


def get_settings():
    try:
        return frappe.get_single("S3 Vault Settings")
    except Exception:
        return None


def get_bucket(connection: str, check_permission: bool = True):
    if check_permission:
        require_system_manager()

    if not connection:
        frappe.throw(_("Select an S3 Vault connection."))

    if not frappe.db.exists("S3 Vault Bucket", connection):
        frappe.throw(_("S3 Vault connection {0} does not exist.").format(connection))

    bucket = frappe.get_doc("S3 Vault Bucket", connection)

    if not cint(bucket.enabled):
        frappe.throw(_("S3 Vault connection {0} is disabled.").format(bucket.name))

    if not bucket.bucket_name:
        frappe.throw(_("Bucket Name is missing on connection {0}.").format(bucket.name))

    return bucket


def normalize_relative_path(value: str | None, folder: bool = False) -> str:
    value = str(value or "").strip().replace("\\", "/")

    if "\x00" in value:
        frappe.throw(_("Invalid path."))

    value = re.sub(r"/+", "/", value).strip("/")
    parts: list[str] = []

    for part in value.split("/"):
        if not part:
            continue
        if part in {".", ".."}:
            frappe.throw(_("Relative path segments are not allowed."))
        parts.append(part)

    normalized = "/".join(parts)

    if folder and normalized:
        normalized += "/"

    return normalized


def root_prefix(bucket) -> str:
    base_prefix = normalize_relative_path(bucket.base_prefix or "")
    return f"{base_prefix}/" if base_prefix else ""


def full_key(bucket, relative_key: str | None = "", folder: bool = False) -> str:
    relative_key = normalize_relative_path(relative_key, folder=folder)
    return f"{root_prefix(bucket)}{relative_key}"


def relative_key(bucket, storage_key: str) -> str:
    root = root_prefix(bucket)

    if root and not storage_key.startswith(root):
        frappe.throw(_("The requested object is outside the configured base prefix."))

    return storage_key[len(root) :] if root else storage_key


def safe_file_name(filename: str | None) -> str:
    filename = str(filename or "").replace("\\", "/")
    filename = os.path.basename(filename).strip()

    if not filename or filename in {".", ".."} or "\x00" in filename:
        frappe.throw(_("Invalid file name."))

    if len(filename.encode("utf-8")) > 255:
        frappe.throw(_("File name is too long."))

    return filename


def safe_folder_name(folder_name: str | None) -> str:
    folder_name = str(folder_name or "").strip()

    if (
        not folder_name
        or folder_name in {".", ".."}
        or "/" in folder_name
        or "\\" in folder_name
        or "\x00" in folder_name
    ):
        frappe.throw(_("Enter one valid folder name without / or \\ characters."))

    if len(folder_name.encode("utf-8")) > 255:
        frappe.throw(_("Folder name is too long."))

    return folder_name


def join_relative(parent: str | None, name: str, folder: bool = False) -> str:
    parent = normalize_relative_path(parent, folder=bool(parent))
    name = safe_folder_name(name) if folder else safe_file_name(name)
    value = f"{parent}{name}"
    return normalize_relative_path(value, folder=folder)


def parent_prefix(value: str | None) -> str:
    normalized = normalize_relative_path(value)
    if not normalized or "/" not in normalized:
        return ""
    return normalized.rsplit("/", 1)[0] + "/"


def basename(value: str | None) -> str:
    normalized = normalize_relative_path(value)
    return normalized.rsplit("/", 1)[-1] if normalized else ""


def folder_basename(value: str | None) -> str:
    normalized = normalize_relative_path(value, folder=bool(value)).rstrip("/")
    return normalized.rsplit("/", 1)[-1] if normalized else ""


def get_error_code(exc: ClientError) -> str:
    try:
        return str(exc.response.get("Error", {}).get("Code", ""))
    except Exception:
        return ""


def object_exists(client, bucket_name: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket_name, Key=key)
        return True
    except ClientError as exc:
        if get_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def folder_exists(client, bucket_name: str, prefix: str) -> bool:
    response = client.list_objects_v2(
        Bucket=bucket_name,
        Prefix=prefix,
        MaxKeys=1,
    )
    return bool(response.get("KeyCount") or response.get("Contents"))


def url_expiry() -> int:
    settings = get_settings()
    configured = cint(getattr(settings, "default_url_expiry_seconds", 0)) if settings else 0
    return max(60, min(configured or DEFAULT_URL_EXPIRY_SECONDS, 3600))


def blocked_extensions() -> set[str]:
    settings = get_settings()
    value = getattr(settings, "global_blocked_extensions", "") if settings else ""
    return {
        item.strip().lower().lstrip(".")
        for item in re.split(r"[\n,;| ]+", str(value or ""))
        if item.strip()
    }


def iso(value):
    return value.isoformat() if value else None


def parse_json(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return frappe.parse_json(value)
    except Exception:
        try:
            return json.loads(value)
        except Exception:
            frappe.throw(_("Invalid JSON data."))


def parse_items(value) -> list[dict]:
    rows = parse_json(value, default=[])
    if not isinstance(rows, list):
        frappe.throw(_("Selected items must be a list."))

    if len(rows) > MAX_BULK_ITEMS:
        frappe.throw(
            _("You can process at most {0} selected rows at one time.").format(MAX_BULK_ITEMS)
        )

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            frappe.throw(_("Each selected item must be an object."))
        item_type = str(row.get("type") or "").strip().lower()
        if item_type not in {"file", "folder"}:
            frappe.throw(_("Selected item type must be file or folder."))
        key = normalize_relative_path(row.get("key"), folder=item_type == "folder")
        if not key:
            frappe.throw(_("Selected item key is required."))
        out.append(
            {
                "type": item_type,
                "key": key,
                "name": folder_basename(key) if item_type == "folder" else basename(key),
            }
        )

    return deduplicate_items(out)


def deduplicate_items(items: list[dict]) -> list[dict]:
    folders = sorted(
        {normalize_relative_path(row["key"], folder=True) for row in items if row["type"] == "folder"},
        key=len,
    )
    retained_folders: list[str] = []
    for prefix in folders:
        if any(prefix.startswith(parent) for parent in retained_folders):
            continue
        retained_folders.append(prefix)

    files = sorted(
        {normalize_relative_path(row["key"]) for row in items if row["type"] == "file"}
    )
    retained_files = [
        key for key in files if not any(key.startswith(prefix) for prefix in retained_folders)
    ]

    return [
        {"type": "folder", "key": key, "name": folder_basename(key)}
        for key in retained_folders
    ] + [
        {"type": "file", "key": key, "name": basename(key)}
        for key in retained_files
    ]


def linked_records_for_keys(bucket, storage_keys: Iterable[str]) -> dict[str, list[dict]]:
    keys = list(dict.fromkeys(str(key) for key in storage_keys if key))
    if not keys:
        return {}

    result: dict[str, list[dict]] = {}
    fields = [
        "name",
        "file",
        "bucket",
        "bucket_name",
        "object_key",
        "stored_file_name",
        "status",
        "attached_to_doctype",
        "attached_to_name",
        "original_file_name",
    ]

    # Large recursive operations can contain tens of thousands of keys. Chunk
    # the IN queries to keep MariaDB query size and parameter counts reasonable.
    chunk_size = 500
    for index in range(0, len(keys), chunk_size):
        chunk = keys[index : index + chunk_size]
        rows = frappe.get_all(
            "S3 Vault File",
            filters={"object_key": ["in", chunk]},
            fields=fields,
            limit=max(500, len(chunk) * 10),
        )

        for row in rows:
            if row.bucket and row.bucket != bucket.name:
                continue
            if row.bucket_name and row.bucket_name != bucket.bucket_name:
                continue
            if row.status == "Deleted":
                continue
            result.setdefault(row.object_key, []).append(dict(row))

    return result


def first_linked_record(bucket, storage_key: str) -> dict | None:
    rows = linked_records_for_keys(bucket, [storage_key]).get(storage_key, [])
    return rows[0] if rows else None


def update_linked_records_after_move(
    bucket,
    key_map: dict[str, str],
    metadata_by_destination: dict[str, dict] | None = None,
) -> int:
    if not key_map:
        return 0

    metadata_by_destination = metadata_by_destination or {}
    linked = linked_records_for_keys(bucket, key_map.keys())
    updated = 0

    for source_key, records in linked.items():
        destination_key = key_map.get(source_key)
        if not destination_key:
            continue
        destination_metadata = metadata_by_destination.get(destination_key, {})

        for row in records:
            values = {
                "object_key": destination_key,
                "stored_file_name": os.path.basename(destination_key),
            }
            etag = str(destination_metadata.get("ETag") or "").strip('"')
            if etag:
                values["etag"] = etag
            if destination_metadata.get("VersionId"):
                values["version_id"] = destination_metadata.get("VersionId")
            meta = frappe.get_meta("S3 Vault File")
            values = {key: value for key, value in values.items() if meta.has_field(key)}
            if values:
                frappe.db.set_value(
                    "S3 Vault File",
                    row["name"],
                    values,
                    update_modified=True,
                )
                updated += 1

    return updated


def mark_linked_records_deleted(bucket, storage_keys: Iterable[str]) -> int:
    linked = linked_records_for_keys(bucket, storage_keys)
    updated = 0

    for records in linked.values():
        for row in records:
            meta = frappe.get_meta("S3 Vault File")
            values = {
                "status": "Deleted",
                "deleted_from_storage": 1,
                "deleted_on": frappe.utils.now(),
                "deleted_by": getattr(frappe.session, "user", None) or "Administrator",
            }
            values = {key: value for key, value in values.items() if meta.has_field(key)}
            if values:
                frappe.db.set_value(
                    "S3 Vault File",
                    row["name"],
                    values,
                    update_modified=True,
                )
                updated += 1
    return updated


def manager_log(
    *,
    action: str,
    bucket,
    object_key: str | None = None,
    source_key: str | None = None,
    destination_key: str | None = None,
    status: str = "Success",
    user: str | None = None,
    message: str | None = None,
    traceback_text: str | None = None,
    commit: bool = False,
):
    details = []
    if source_key:
        details.append(f"source={source_key}")
    if destination_key:
        details.append(f"destination={destination_key}")
    if message:
        details.append(str(message))

    return write_log(
        action=action,
        status=status,
        bucket_name=bucket.bucket_name,
        object_key=object_key or destination_key or source_key,
        error_message="; ".join(details) or None,
        traceback_text=traceback_text,
        user=user,
        commit=commit,
    )


def content_type_for_name(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def operation_as_dict(doc) -> dict:
    return {
        "name": doc.name,
        "operation_type": doc.operation_type,
        "connection": doc.connection,
        "bucket_name": doc.bucket_name,
        "source_key": doc.source_key,
        "destination_key": doc.destination_key,
        "status": doc.status,
        "progress": float(doc.progress or 0),
        "total_objects": cint(doc.total_objects),
        "processed_objects": cint(doc.processed_objects),
        "failed_objects": cint(doc.failed_objects),
        "total_size": doc.total_size,
        "processed_size": doc.processed_size,
        "message": doc.message,
        "started_by": doc.started_by,
        "started_on": iso(doc.started_on),
        "completed_on": iso(doc.completed_on),
        "error_message": doc.error_message,
        "result_key": doc.result_key,
        "result_file_name": doc.result_file_name,
        "result_expires_on": iso(doc.result_expires_on),
        "result_deleted": cint(doc.result_deleted),
        "background_job_id": doc.background_job_id,
        "cancellation_requested": cint(getattr(doc, "cancellation_requested", 0)),
        "retry_of": getattr(doc, "retry_of", None),
        "retry_count": cint(getattr(doc, "retry_count", 0)),
        "creation": iso(doc.creation),
        "modified": iso(doc.modified),
    }


def format_bytes(value: int | float | str | None) -> str:
    try:
        size = float(value or 0)
    except Exception:
        size = 0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.0f} {units[index]}" if index == 0 else f"{size:.1f} {units[index]}"


def parse_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        return frappe.utils.get_datetime(value)
    except Exception:
        return None


def get_s3_client(bucket):
    return s3_client(bucket)
