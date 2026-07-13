from __future__ import annotations

import mimetypes
import os
import re
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from frappe_s3_vault.file_manager_common import (
    DEFAULT_LIST_PAGE_SIZE,
    MAX_DIRECT_UPLOAD_BYTES,
    MAX_FOLDER_SUMMARY_OBJECTS,
    MAX_LIST_PAGE_SIZE,
    TEMP_ARCHIVE_RETENTION_HOURS,
    basename,
    blocked_extensions,
    content_type_for_name,
    first_linked_record,
    folder_basename,
    folder_exists,
    format_bytes,
    full_key,
    get_bucket,
    get_s3_client,
    get_settings,
    iso,
    linked_records_for_keys,
    manager_log,
    normalize_relative_path,
    object_exists,
    operation_as_dict,
    parse_items,
    relative_key,
    require_system_manager,
    root_prefix,
    safe_file_name,
    safe_folder_name,
    url_expiry,
)
from frappe_s3_vault.file_manager_operations import (
    delete_file as delete_file_now,
    get_folder_summary as build_folder_summary,
    rename_or_transfer_file,
)

ALLOWED_BACKGROUND_OPERATIONS = {
    "Bulk Copy",
    "Bulk Move",
    "Bulk Delete",
    "Rename Folder",
    "Copy Folder",
    "Move Folder",
    "Delete Folder",
    "Download Folder ZIP",
    "Bulk Download ZIP",
}
ALLOWED_CONFLICT_STRATEGIES = {"fail", "replace", "keep_both"}


def _linked_payload(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "storage_file": row.get("name"),
        "file": row.get("file"),
        "attached_to_doctype": row.get("attached_to_doctype"),
        "attached_to_name": row.get("attached_to_name"),
        "original_file_name": row.get("original_file_name"),
        "status": row.get("status"),
    }


def _operation_doc(operation_name: str):
    require_system_manager()
    if not operation_name or not frappe.db.exists("S3 Vault Operation", operation_name):
        frappe.throw(_("S3 Vault Operation does not exist."))
    doc = frappe.get_doc("S3 Vault Operation", operation_name)
    if doc.started_by != frappe.session.user and "System Manager" not in frappe.get_roles():
        frappe.throw(_("You cannot view this operation."), frappe.PermissionError)
    return doc


def _create_operation(
    *,
    operation_type: str,
    bucket,
    source_key: str | None,
    destination_key: str | None,
    payload: dict,
):
    if operation_type not in ALLOWED_BACKGROUND_OPERATIONS:
        frappe.throw(_("Unsupported operation type: {0}").format(operation_type))

    doc = frappe.new_doc("S3 Vault Operation")
    doc.operation_type = operation_type
    doc.connection = bucket.name
    doc.bucket_name = bucket.bucket_name
    doc.source_key = source_key
    doc.destination_key = destination_key
    doc.status = "Queued"
    doc.progress = 0
    doc.total_objects = 0
    doc.processed_objects = 0
    doc.failed_objects = 0
    doc.total_size = "0"
    doc.processed_size = "0"
    doc.started_by = frappe.session.user
    doc.message = _("Waiting for a background worker")
    doc.operation_payload = frappe.as_json(payload)
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)

    job_id = f"s3vault-operation-{doc.name}"[:140]
    frappe.db.set_value(
        "S3 Vault Operation",
        doc.name,
        "background_job_id",
        job_id,
        update_modified=False,
    )

    frappe.enqueue(
        "frappe_s3_vault.file_manager_jobs.run_operation",
        queue="long",
        timeout=21_600,
        job_id=job_id,
        enqueue_after_commit=True,
        operation_name=doc.name,
    )
    doc.reload()
    return operation_as_dict(doc)


@frappe.whitelist()
def get_connections():
    """Return enabled S3 connections without exposing credentials."""
    require_system_manager()

    rows = frappe.get_all(
        "S3 Vault Bucket",
        filters={"enabled": 1},
        fields=[
            "name",
            "bucket_title",
            "bucket_name",
            "provider_type",
            "region",
            "endpoint_url",
            "base_prefix",
            "is_default",
        ],
        order_by="is_default desc, bucket_title asc",
    )

    settings = get_settings()
    default_connection = getattr(settings, "default_bucket", None) if settings else None

    enabled_names = {row.name for row in rows}
    if default_connection not in enabled_names:
        default_connection = next((row.name for row in rows if cint(row.is_default)), None)

    if not default_connection and rows:
        default_connection = rows[0].name

    return {
        "connections": rows,
        "default_connection": default_connection,
    }


@frappe.whitelist()
def list_objects(
    connection: str,
    prefix: str | None = "",
    continuation_token: str | None = None,
    page_size: int = DEFAULT_LIST_PAGE_SIZE,
):
    """List immediate folders and files below a relative prefix."""
    bucket = get_bucket(connection)
    client = get_s3_client(bucket)

    relative_prefix = normalize_relative_path(prefix, folder=bool(prefix))
    storage_prefix = full_key(bucket, relative_prefix, folder=bool(relative_prefix))
    page_size = max(1, min(cint(page_size) or DEFAULT_LIST_PAGE_SIZE, MAX_LIST_PAGE_SIZE))

    params = {
        "Bucket": bucket.bucket_name,
        "Prefix": storage_prefix,
        "Delimiter": "/",
        "MaxKeys": page_size,
    }
    if continuation_token:
        params["ContinuationToken"] = continuation_token

    response = client.list_objects_v2(**params)

    folders = []
    for item in response.get("CommonPrefixes", []):
        storage_folder_key = item.get("Prefix") or ""
        relative_folder_key = relative_key(bucket, storage_folder_key)
        folder_name = folder_basename(relative_folder_key)
        if folder_name:
            folders.append(
                {
                    "name": folder_name,
                    "key": relative_folder_key,
                    "type": "folder",
                }
            )

    file_rows = []
    storage_file_keys = []
    for item in response.get("Contents", []):
        storage_object_key = item.get("Key") or ""
        if (
            not storage_object_key
            or storage_object_key == storage_prefix
            or storage_object_key.endswith("/")
        ):
            continue

        relative_object_key = relative_key(bucket, storage_object_key)
        filename = basename(relative_object_key)
        storage_file_keys.append(storage_object_key)
        file_rows.append(
            {
                "name": filename,
                "key": relative_object_key,
                "storage_key": storage_object_key,
                "type": "file",
                "size": cint(item.get("Size")),
                "last_modified": iso(item.get("LastModified")),
                "etag": str(item.get("ETag") or "").strip('"'),
                "storage_class": item.get("StorageClass"),
                "content_type": content_type_for_name(filename),
            }
        )

    linked = linked_records_for_keys(bucket, storage_file_keys)
    for row in file_rows:
        row["linked"] = _linked_payload((linked.get(row.pop("storage_key")) or [None])[0])

    folders.sort(key=lambda row: row["name"].lower())
    file_rows.sort(key=lambda row: row["name"].lower())

    return {
        "connection": bucket.name,
        "bucket_title": bucket.bucket_title,
        "bucket_name": bucket.bucket_name,
        "provider_type": bucket.provider_type,
        "base_prefix": root_prefix(bucket),
        "prefix": relative_prefix,
        "folders": folders,
        "files": file_rows,
        "is_truncated": bool(response.get("IsTruncated")),
        "next_token": response.get("NextContinuationToken"),
        "key_count": cint(response.get("KeyCount")),
    }


@frappe.whitelist()
def list_folders(
    connection: str,
    prefix: str | None = "",
):
    """Small folder-only response used by destination picker dialogs."""
    result = list_objects(
        connection=connection,
        prefix=prefix,
        page_size=MAX_LIST_PAGE_SIZE,
    )
    return {
        "prefix": result["prefix"],
        "folders": result["folders"],
        "is_truncated": result["is_truncated"],
    }


@frappe.whitelist()
def create_folder(connection: str, prefix: str | None, folder_name: str):
    bucket = get_bucket(connection)
    client = get_s3_client(bucket)

    parent = normalize_relative_path(prefix, folder=bool(prefix))
    folder_name = safe_folder_name(folder_name)
    relative_folder = normalize_relative_path(f"{parent}{folder_name}", folder=True)
    storage_folder = full_key(bucket, relative_folder, folder=True)

    if folder_exists(client, bucket.bucket_name, storage_folder):
        frappe.throw(_("Folder {0} already exists.").format(folder_name))

    client.put_object(
        Bucket=bucket.bucket_name,
        Key=storage_folder,
        Body=b"",
        ContentType="application/x-directory",
    )
    manager_log(
        action="Upload",
        bucket=bucket,
        object_key=storage_folder,
        message="S3 File Manager created a folder marker",
        user=frappe.session.user,
    )
    return {"name": folder_name, "key": relative_folder}


@frappe.whitelist()
def get_object_url(
    connection: str,
    key: str,
    disposition: str = "inline",
):
    bucket = get_bucket(connection)
    client = get_s3_client(bucket)

    relative_object_key = normalize_relative_path(key)
    if not relative_object_key:
        frappe.throw(_("Object key is required."))

    storage_key = full_key(bucket, relative_object_key)
    metadata = client.head_object(Bucket=bucket.bucket_name, Key=storage_key)

    mode = "attachment" if disposition == "attachment" else "inline"
    filename = safe_file_name(os.path.basename(relative_object_key))
    response_disposition = f"{mode}; filename*=UTF-8''{quote(filename)}"
    expiry = url_expiry()

    url = client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket.bucket_name,
            "Key": storage_key,
            "ResponseContentDisposition": response_disposition,
        },
        ExpiresIn=expiry,
    )

    manager_log(
        action="Download" if mode == "attachment" else "Preview",
        bucket=bucket,
        object_key=storage_key,
        user=frappe.session.user,
    )

    return {
        "url": url,
        "expires_in": expiry,
        "name": filename,
        "key": relative_object_key,
        "content_type": metadata.get("ContentType")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream",
        "size": cint(metadata.get("ContentLength")),
        "etag": str(metadata.get("ETag") or "").strip('"'),
        "last_modified": iso(metadata.get("LastModified")),
    }


@frappe.whitelist()
def get_object_details(connection: str, key: str):
    bucket = get_bucket(connection)
    client = get_s3_client(bucket)
    relative_object_key = normalize_relative_path(key)
    storage_key = full_key(bucket, relative_object_key)
    metadata = client.head_object(Bucket=bucket.bucket_name, Key=storage_key)
    linked = first_linked_record(bucket, storage_key)

    tags = []
    try:
        tags = client.get_object_tagging(
            Bucket=bucket.bucket_name,
            Key=storage_key,
        ).get("TagSet", [])
    except Exception:
        pass

    return {
        "name": basename(relative_object_key),
        "key": relative_object_key,
        "size": cint(metadata.get("ContentLength")),
        "content_type": metadata.get("ContentType") or content_type_for_name(relative_object_key),
        "last_modified": iso(metadata.get("LastModified")),
        "etag": str(metadata.get("ETag") or "").strip('"'),
        "version_id": metadata.get("VersionId"),
        "cache_control": metadata.get("CacheControl"),
        "content_disposition": metadata.get("ContentDisposition"),
        "server_side_encryption": metadata.get("ServerSideEncryption"),
        "storage_class": metadata.get("StorageClass"),
        "metadata": metadata.get("Metadata") or {},
        "tags": tags,
        "linked": _linked_payload(linked),
    }


@frappe.whitelist()
def get_folder_summary(
    connection: str,
    prefix: str,
    max_objects: int = MAX_FOLDER_SUMMARY_OBJECTS,
):
    bucket = get_bucket(connection)
    max_objects = max(1, min(cint(max_objects) or MAX_FOLDER_SUMMARY_OBJECTS, 50_000))
    summary = build_folder_summary(bucket, prefix, max_objects)
    summary["formatted_size"] = format_bytes(summary["total_bytes"])
    return summary


@frappe.whitelist()
def create_upload_session(
    connection: str,
    prefix: str | None,
    filename: str,
    content_type: str | None = None,
    file_size: int = 0,
    overwrite: int = 0,
):
    bucket = get_bucket(connection)
    client = get_s3_client(bucket)

    parent = normalize_relative_path(prefix, folder=bool(prefix))
    filename = safe_file_name(filename)
    file_size = cint(file_size)

    if file_size <= 0:
        frappe.throw(_("The selected file is empty."))
    if file_size > MAX_DIRECT_UPLOAD_BYTES:
        frappe.throw(
            _("Direct upload is limited to 500 MB per file. Multipart upload is planned for Phase 3.")
        )

    extension = os.path.splitext(filename)[1].lower().lstrip(".")
    if extension and extension in blocked_extensions():
        frappe.throw(_("File type .{0} is blocked by S3 Vault Settings.").format(extension))

    relative_object_key = normalize_relative_path(f"{parent}{filename}")
    storage_key = full_key(bucket, relative_object_key)

    if not cint(overwrite) and object_exists(client, bucket.bucket_name, storage_key):
        frappe.throw(_("A file named {0} already exists in this folder.").format(filename))

    content_type = (
        str(content_type or "").strip()
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    expiry = min(url_expiry(), 900)
    upload_url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket.bucket_name,
            "Key": storage_key,
            "ContentType": content_type,
        },
        ExpiresIn=expiry,
    )

    return {
        "upload_url": upload_url,
        "method": "PUT",
        "headers": {"Content-Type": content_type},
        "expires_in": expiry,
        "name": filename,
        "key": relative_object_key,
        "expected_size": file_size,
        "content_type": content_type,
    }


@frappe.whitelist()
def complete_upload(connection: str, key: str, expected_size: int = 0):
    bucket = get_bucket(connection)
    client = get_s3_client(bucket)
    relative_object_key = normalize_relative_path(key)
    storage_key = full_key(bucket, relative_object_key)
    metadata = client.head_object(Bucket=bucket.bucket_name, Key=storage_key)

    actual_size = cint(metadata.get("ContentLength"))
    expected_size = cint(expected_size)
    if expected_size and actual_size != expected_size:
        frappe.throw(
            _("Upload size verification failed. Expected {0} bytes but S3 contains {1} bytes.").format(
                expected_size,
                actual_size,
            )
        )

    manager_log(
        action="Upload",
        bucket=bucket,
        object_key=storage_key,
        user=frappe.session.user,
    )
    filename = basename(relative_object_key)
    return {
        "name": filename,
        "key": relative_object_key,
        "size": actual_size,
        "content_type": metadata.get("ContentType") or content_type_for_name(filename),
        "etag": str(metadata.get("ETag") or "").strip('"'),
        "last_modified": iso(metadata.get("LastModified")),
    }


@frappe.whitelist()
def rename_file(
    connection: str,
    key: str,
    new_name: str,
    conflict_strategy: str = "fail",
    update_linked_record: int = 1,
):
    bucket = get_bucket(connection)
    if conflict_strategy not in ALLOWED_CONFLICT_STRATEGIES:
        frappe.throw(_("Invalid conflict strategy."))
    return rename_or_transfer_file(
        bucket=bucket,
        source_relative_key=key,
        destination_parent=normalize_relative_path(key).rsplit("/", 1)[0] + "/"
        if "/" in normalize_relative_path(key)
        else "",
        new_name=new_name,
        mode="move",
        conflict_strategy=conflict_strategy,
        update_linked_record=bool(cint(update_linked_record)),
        user=frappe.session.user,
    )


@frappe.whitelist()
def copy_file(
    connection: str,
    key: str,
    destination_prefix: str,
    new_name: str | None = None,
    conflict_strategy: str = "fail",
):
    bucket = get_bucket(connection)
    if conflict_strategy not in ALLOWED_CONFLICT_STRATEGIES:
        frappe.throw(_("Invalid conflict strategy."))
    return rename_or_transfer_file(
        bucket=bucket,
        source_relative_key=key,
        destination_parent=destination_prefix,
        new_name=new_name,
        mode="copy",
        conflict_strategy=conflict_strategy,
        update_linked_record=False,
        user=frappe.session.user,
    )


@frappe.whitelist()
def move_file(
    connection: str,
    key: str,
    destination_prefix: str,
    new_name: str | None = None,
    conflict_strategy: str = "fail",
    update_linked_record: int = 1,
):
    bucket = get_bucket(connection)
    if conflict_strategy not in ALLOWED_CONFLICT_STRATEGIES:
        frappe.throw(_("Invalid conflict strategy."))
    return rename_or_transfer_file(
        bucket=bucket,
        source_relative_key=key,
        destination_parent=destination_prefix,
        new_name=new_name,
        mode="move",
        conflict_strategy=conflict_strategy,
        update_linked_record=bool(cint(update_linked_record)),
        user=frappe.session.user,
    )


@frappe.whitelist()
def delete_file(
    connection: str,
    key: str,
    allow_linked_delete: int = 0,
    confirmation: str | None = None,
):
    bucket = get_bucket(connection)
    filename = basename(key)
    if confirmation != filename:
        frappe.throw(_("Type the exact file name to confirm deletion."))
    return delete_file_now(
        bucket=bucket,
        source_relative_key=key,
        allow_linked_delete=bool(cint(allow_linked_delete)),
        user=frappe.session.user,
    )


@frappe.whitelist()
def create_background_operation(
    connection: str,
    operation_type: str,
    items=None,
    source_prefix: str | None = None,
    destination_prefix: str | None = None,
    new_name: str | None = None,
    conflict_strategy: str = "fail",
    update_linked_records: int = 1,
    allow_linked_delete: int = 0,
    confirmation: str | None = None,
):
    bucket = get_bucket(connection)
    operation_type = str(operation_type or "").strip()
    if operation_type not in ALLOWED_BACKGROUND_OPERATIONS:
        frappe.throw(_("Unsupported operation type."))
    if conflict_strategy not in ALLOWED_CONFLICT_STRATEGIES:
        frappe.throw(_("Invalid conflict strategy."))

    selected_items = parse_items(items)
    source_prefix = normalize_relative_path(
        source_prefix,
        folder=bool(source_prefix),
    )
    destination_prefix = normalize_relative_path(
        destination_prefix,
        folder=bool(destination_prefix),
    )

    if operation_type in {
        "Rename Folder",
        "Copy Folder",
        "Move Folder",
        "Delete Folder",
        "Download Folder ZIP",
    }:
        if not source_prefix:
            frappe.throw(_("Source folder is required."))
        source_name = folder_basename(source_prefix)
        selected_items = [{"type": "folder", "key": source_prefix, "name": source_name}]

    if operation_type in {"Rename Folder", "Copy Folder", "Move Folder"}:
        if operation_type == "Rename Folder":
            new_name = safe_folder_name(new_name)
            destination_prefix = (
                source_prefix.rstrip("/").rsplit("/", 1)[0] + "/"
                if "/" in source_prefix.rstrip("/")
                else ""
            )
        if not new_name and operation_type != "Rename Folder":
            new_name = None

    if operation_type in {"Bulk Copy", "Bulk Move", "Bulk Delete", "Bulk Download ZIP"}:
        if not selected_items:
            frappe.throw(_("Select at least one file or folder."))

    if operation_type in {"Delete Folder", "Bulk Delete"}:
        expected = folder_basename(source_prefix) if operation_type == "Delete Folder" else "DELETE"
        if confirmation != expected:
            frappe.throw(_("Deletion confirmation does not match."))

    payload = {
        "items": selected_items,
        "destination_prefix": destination_prefix,
        "new_name": new_name,
        "conflict_strategy": conflict_strategy,
        "update_linked_records": bool(cint(update_linked_records)),
        "allow_linked_delete": bool(cint(allow_linked_delete)),
        "requested_by": frappe.session.user,
    }
    return _create_operation(
        operation_type=operation_type,
        bucket=bucket,
        source_key=source_prefix or (selected_items[0]["key"] if len(selected_items) == 1 else None),
        destination_key=destination_prefix or None,
        payload=payload,
    )


@frappe.whitelist()
def get_operation_status(operation_name: str):
    return operation_as_dict(_operation_doc(operation_name))


@frappe.whitelist()
def get_recent_operations(connection: str | None = None, limit: int = 10):
    require_system_manager()
    filters = {}
    if connection:
        filters["connection"] = connection
    rows = frappe.get_all(
        "S3 Vault Operation",
        filters=filters,
        fields=["name"],
        order_by="creation desc",
        limit=max(1, min(cint(limit) or 10, 50)),
    )
    return [operation_as_dict(frappe.get_doc("S3 Vault Operation", row.name)) for row in rows]


@frappe.whitelist()
def get_operation_result_url(operation_name: str):
    doc = _operation_doc(operation_name)
    if doc.status != "Completed" or not doc.result_key or cint(doc.result_deleted):
        frappe.throw(_("This operation has no available result file."))
    if doc.result_expires_on and now_datetime() > frappe.utils.get_datetime(doc.result_expires_on):
        frappe.throw(_("The generated archive has expired."))

    bucket = get_bucket(doc.connection)
    client = get_s3_client(bucket)
    expiry = min(url_expiry(), 900)
    filename = safe_file_name(doc.result_file_name or basename(doc.result_key))
    url = client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket.bucket_name,
            "Key": full_key(bucket, doc.result_key),
            "ResponseContentDisposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        },
        ExpiresIn=expiry,
    )
    manager_log(
        action="Download",
        bucket=bucket,
        object_key=full_key(bucket, doc.result_key),
        user=frappe.session.user,
        message=f"operation={doc.name}",
    )
    return {"url": url, "expires_in": expiry, "filename": filename}


@frappe.whitelist()
def open_linked_record(connection: str, key: str):
    bucket = get_bucket(connection)
    storage_key = full_key(bucket, normalize_relative_path(key))
    linked = first_linked_record(bucket, storage_key)
    if not linked:
        return None
    return _linked_payload(linked)
