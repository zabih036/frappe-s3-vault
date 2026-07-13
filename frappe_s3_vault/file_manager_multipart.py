from __future__ import annotations

import hashlib
import math
import mimetypes
import os
from typing import Iterable

import frappe
from botocore.exceptions import ClientError
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from frappe_s3_vault.file_manager_common import (
    basename,
    blocked_extensions,
    content_type_for_name,
    full_key,
    get_bucket,
    get_error_code,
    get_s3_client,
    manager_log,
    normalize_relative_path,
    relative_key,
    safe_file_name,
    url_expiry,
)
from frappe_s3_vault.file_manager_operations import resolve_file_destination
from frappe_s3_vault.file_manager_permissions import is_admin, require_access

MIN_PART_SIZE = 8 * 1024 * 1024
MAX_PART_SIZE = 5 * 1024 * 1024 * 1024
MAX_PARTS = 10_000
MAX_OBJECT_SIZE = 5 * 1024 * 1024 * 1024 * 1024
MULTIPART_EXPIRY_HOURS = 48
PRESIGN_BATCH_LIMIT = 100


def choose_part_size(file_size: int) -> int:
    file_size = int(file_size)
    required = math.ceil(file_size / MAX_PARTS)
    part_size = max(MIN_PART_SIZE, required)
    # Round to a whole MiB so browser slices are predictable.
    mib = 1024 * 1024
    part_size = math.ceil(part_size / mib) * mib
    if part_size > MAX_PART_SIZE:
        frappe.throw(_("The file cannot be represented within the S3 multipart part limit."))
    return part_size


def _safe_relative_upload_path(prefix: str | None, relative_path: str | None, filename: str) -> str:
    parent = normalize_relative_path(prefix, folder=bool(prefix))
    filename = safe_file_name(filename)
    supplied = normalize_relative_path(relative_path or filename)
    if supplied:
        parts = supplied.split("/")
        parts[-1] = filename
        supplied = "/".join(parts)
    else:
        supplied = filename
    return normalize_relative_path(f"{parent}{supplied}")


def _get_doc(name: str, user: str | None = None):
    if not name or not frappe.db.exists("S3 Vault Multipart Upload", name):
        frappe.throw(_("Multipart upload session does not exist."))
    doc = frappe.get_doc("S3 Vault Multipart Upload", name)
    user = user or frappe.session.user
    if doc.upload_user != user and not is_admin(user):
        frappe.throw(_("You cannot access this multipart upload."), frappe.PermissionError)
    require_access(doc.connection, doc.relative_key, "upload", user)
    return doc


def _list_parts(client, bucket_name: str, object_key: str, upload_id: str) -> list[dict]:
    parts: list[dict] = []
    marker = 0
    while True:
        response = client.list_parts(
            Bucket=bucket_name,
            Key=object_key,
            UploadId=upload_id,
            PartNumberMarker=marker,
            MaxParts=1000,
        )
        for row in response.get("Parts", []):
            parts.append(
                {
                    "PartNumber": int(row["PartNumber"]),
                    "ETag": str(row["ETag"]),
                    "Size": int(row.get("Size") or 0),
                    "LastModified": row.get("LastModified").isoformat()
                    if row.get("LastModified")
                    else None,
                }
            )
        if not response.get("IsTruncated"):
            break
        marker = int(response.get("NextPartNumberMarker") or 0)
    return sorted(parts, key=lambda row: row["PartNumber"])


def _session_payload(doc, parts: list[dict] | None = None) -> dict:
    parts = parts or []
    return {
        "name": doc.name,
        "status": doc.status,
        "connection": doc.connection,
        "relative_key": doc.relative_key,
        "file_name": doc.file_name,
        "file_size": cint(doc.file_size),
        "content_type": doc.content_type,
        "file_fingerprint": doc.file_fingerprint,
        "part_size": cint(doc.part_size),
        "total_parts": cint(doc.total_parts),
        "uploaded_parts": len(parts) if parts else cint(doc.uploaded_parts),
        "uploaded_size": sum(int(row.get("Size") or 0) for row in parts)
        if parts
        else cint(doc.uploaded_size),
        "parts": parts,
        "expires_on": doc.expires_on,
    }


@frappe.whitelist()
def create_session(
    connection: str,
    prefix: str | None,
    filename: str,
    file_size: int,
    content_type: str | None = None,
    relative_path: str | None = None,
    file_fingerprint: str | None = None,
    conflict_strategy: str = "fail",
):
    file_size = cint(file_size)
    if file_size <= 0:
        frappe.throw(_("The selected file is empty."))
    if file_size > MAX_OBJECT_SIZE:
        frappe.throw(_("The file exceeds the 5 TB S3 multipart object limit."))
    if conflict_strategy not in {"fail", "replace", "keep_both"}:
        frappe.throw(_("Invalid conflict strategy."))

    filename = safe_file_name(filename)
    extension = os.path.splitext(filename)[1].lower().lstrip(".")
    if extension and extension in blocked_extensions():
        frappe.throw(_("File type .{0} is blocked by S3 Vault Settings.").format(extension))

    desired_relative_key = _safe_relative_upload_path(prefix, relative_path, filename)
    require_access(connection, desired_relative_key, "upload")
    bucket = get_bucket(connection, check_permission=False)
    client = get_s3_client(bucket)

    fingerprint = str(file_fingerprint or "").strip()[:240]
    existing_rows = frappe.get_all(
        "S3 Vault Multipart Upload",
        filters={
            "connection": connection,
            "upload_user": frappe.session.user,
            "file_fingerprint": fingerprint,
            "file_size": str(file_size),
            "status": ["in", ["Initiated", "Uploading"]],
            "expires_on": [">", now_datetime()],
        },
        fields=["name"],
        order_by="creation desc",
        limit=5,
    ) if fingerprint else []

    for row in existing_rows:
        doc = frappe.get_doc("S3 Vault Multipart Upload", row.name)
        if doc.relative_key != desired_relative_key:
            continue
        try:
            parts = _list_parts(client, bucket.bucket_name, doc.object_key, doc.upload_id)
            doc.uploaded_parts = len(parts)
            doc.uploaded_size = str(sum(int(part.get("Size") or 0) for part in parts))
            doc.completed_parts_json = frappe.as_json(parts)
            doc.status = "Uploading" if parts else "Initiated"
            doc.save(ignore_permissions=True)
            return _session_payload(doc, parts)
        except Exception:
            doc.status = "Failed"
            doc.error_message = _("The saved multipart upload no longer exists in S3.")
            doc.save(ignore_permissions=True)

    destination_storage_key = resolve_file_destination(
        bucket, desired_relative_key, conflict_strategy
    )
    destination_relative_key = relative_key(bucket, destination_storage_key)
    content_type = (
        str(content_type or "").strip()
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    response = client.create_multipart_upload(
        Bucket=bucket.bucket_name,
        Key=destination_storage_key,
        ContentType=content_type,
        Metadata={
            "s3-vault-upload-user": hashlib.sha256(
                str(frappe.session.user).encode("utf-8")
            ).hexdigest()[:24]
        },
    )
    part_size = choose_part_size(file_size)
    total_parts = math.ceil(file_size / part_size)
    expires_on = add_to_date(now_datetime(), hours=MULTIPART_EXPIRY_HOURS, as_datetime=True)

    doc = frappe.get_doc(
        {
            "doctype": "S3 Vault Multipart Upload",
            "status": "Initiated",
            "connection": bucket.name,
            "bucket_name": bucket.bucket_name,
            "upload_user": frappe.session.user,
            "file_name": basename(destination_relative_key),
            "file_size": str(file_size),
            "relative_key": destination_relative_key,
            "object_key": destination_storage_key,
            "content_type": content_type,
            "file_fingerprint": fingerprint,
            "upload_id": response["UploadId"],
            "part_size": part_size,
            "total_parts": total_parts,
            "uploaded_parts": 0,
            "uploaded_size": "0",
            "completed_parts_json": "[]",
            "initiated_on": now_datetime(),
            "expires_on": expires_on,
        }
    )
    doc.insert(ignore_permissions=True)
    return _session_payload(doc, [])


@frappe.whitelist()
def get_part_urls(session_name: str, part_numbers=None):
    doc = _get_doc(session_name)
    if doc.status not in {"Initiated", "Uploading"}:
        frappe.throw(_("This multipart upload is not active."))
    if doc.expires_on and now_datetime() > get_datetime(doc.expires_on):
        frappe.throw(_("This multipart upload has expired."))

    numbers = frappe.parse_json(part_numbers) if isinstance(part_numbers, str) else part_numbers
    if not isinstance(numbers, list) or not numbers:
        frappe.throw(_("Part numbers are required."))
    numbers = sorted({cint(value) for value in numbers})
    if len(numbers) > PRESIGN_BATCH_LIMIT:
        frappe.throw(_("Request at most {0} part URLs at a time.").format(PRESIGN_BATCH_LIMIT))
    if numbers[0] < 1 or numbers[-1] > cint(doc.total_parts):
        frappe.throw(_("One or more part numbers are outside the upload range."))

    bucket = get_bucket(doc.connection, check_permission=False)
    client = get_s3_client(bucket)
    expiry = min(url_expiry(), 900)
    return {
        "session": doc.name,
        "expires_in": expiry,
        "parts": [
            {
                "part_number": number,
                "url": client.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": bucket.bucket_name,
                        "Key": doc.object_key,
                        "UploadId": doc.upload_id,
                        "PartNumber": number,
                    },
                    ExpiresIn=expiry,
                ),
            }
            for number in numbers
        ],
    }


@frappe.whitelist()
def refresh_session(session_name: str):
    doc = _get_doc(session_name)
    bucket = get_bucket(doc.connection, check_permission=False)
    client = get_s3_client(bucket)
    parts = _list_parts(client, bucket.bucket_name, doc.object_key, doc.upload_id)
    doc.uploaded_parts = len(parts)
    doc.uploaded_size = str(sum(int(row.get("Size") or 0) for row in parts))
    doc.completed_parts_json = frappe.as_json(parts)
    doc.status = "Uploading" if parts else "Initiated"
    doc.save(ignore_permissions=True)
    return _session_payload(doc, parts)


@frappe.whitelist()
def complete_session(session_name: str):
    doc = _get_doc(session_name)
    bucket = get_bucket(doc.connection, check_permission=False)
    client = get_s3_client(bucket)
    parts = _list_parts(client, bucket.bucket_name, doc.object_key, doc.upload_id)
    expected_parts = cint(doc.total_parts)
    if len(parts) != expected_parts:
        frappe.throw(
            _("Multipart upload is incomplete: {0} of {1} parts are present.").format(
                len(parts), expected_parts
            )
        )
    expected_numbers = list(range(1, expected_parts + 1))
    actual_numbers = [int(row["PartNumber"]) for row in parts]
    if actual_numbers != expected_numbers:
        frappe.throw(_("Multipart upload part numbers are incomplete or out of order."))

    response = client.complete_multipart_upload(
        Bucket=bucket.bucket_name,
        Key=doc.object_key,
        UploadId=doc.upload_id,
        MultipartUpload={
            "Parts": [
                {"PartNumber": int(row["PartNumber"]), "ETag": row["ETag"]}
                for row in parts
            ]
        },
    )
    metadata = client.head_object(Bucket=bucket.bucket_name, Key=doc.object_key)
    actual_size = int(metadata.get("ContentLength") or 0)
    if actual_size != cint(doc.file_size):
        frappe.throw(
            _("Completed object size does not match the selected file size."),
        )

    doc.status = "Completed"
    doc.uploaded_parts = len(parts)
    doc.uploaded_size = str(actual_size)
    doc.completed_parts_json = frappe.as_json(parts)
    doc.completed_on = now_datetime()
    doc.error_message = None
    doc.save(ignore_permissions=True)

    manager_log(
        action="Upload",
        bucket=bucket,
        object_key=doc.object_key,
        user=doc.upload_user,
        message=f"multipart_upload={doc.name}; parts={len(parts)}",
    )
    try:
        from frappe_s3_vault.file_manager_index import sync_single_object

        sync_single_object(bucket, doc.object_key)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Index Update After Multipart Upload")

    return {
        "name": doc.file_name,
        "key": doc.relative_key,
        "size": actual_size,
        "content_type": metadata.get("ContentType") or content_type_for_name(doc.file_name),
        "etag": str(metadata.get("ETag") or response.get("ETag") or "").strip('"'),
        "version_id": metadata.get("VersionId") or response.get("VersionId"),
    }


@frappe.whitelist()
def abort_session(session_name: str):
    doc = _get_doc(session_name)
    if doc.status in {"Completed", "Aborted", "Expired"}:
        return {"name": doc.name, "status": doc.status}
    bucket = get_bucket(doc.connection, check_permission=False)
    client = get_s3_client(bucket)
    try:
        client.abort_multipart_upload(
            Bucket=bucket.bucket_name,
            Key=doc.object_key,
            UploadId=doc.upload_id,
        )
    except ClientError as exc:
        if get_error_code(exc) not in {"NoSuchUpload", "404", "NotFound"}:
            raise
    finally:
        doc.status = "Aborted"
        doc.completed_on = now_datetime()
        doc.save(ignore_permissions=True)
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def list_resumable_uploads(connection: str | None = None, prefix: str | None = None):
    filters = {
        "upload_user": frappe.session.user,
        "status": ["in", ["Initiated", "Uploading"]],
        "expires_on": [">", now_datetime()],
    }
    if connection:
        filters["connection"] = connection
    rows = frappe.get_all(
        "S3 Vault Multipart Upload",
        filters=filters,
        fields=["name"],
        order_by="modified desc",
        limit=50,
    )
    output = []
    prefix = normalize_relative_path(prefix, folder=bool(prefix))
    for row in rows:
        doc = frappe.get_doc("S3 Vault Multipart Upload", row.name)
        if prefix and not doc.relative_key.startswith(prefix):
            continue
        if not is_admin() and not doc.upload_user == frappe.session.user:
            continue
        output.append(_session_payload(doc))
    return output


def cleanup_expired_multipart_uploads():
    rows = frappe.get_all(
        "S3 Vault Multipart Upload",
        filters={
            "status": ["in", ["Initiated", "Uploading"]],
            "expires_on": ["<", now_datetime()],
        },
        fields=["name", "connection", "bucket_name", "object_key", "upload_id"],
        limit=200,
    )
    for row in rows:
        try:
            bucket = get_bucket(row.connection, check_permission=False)
            client = get_s3_client(bucket)
            try:
                client.abort_multipart_upload(
                    Bucket=bucket.bucket_name,
                    Key=row.object_key,
                    UploadId=row.upload_id,
                )
            except ClientError as exc:
                if get_error_code(exc) not in {"NoSuchUpload", "404", "NotFound"}:
                    raise
            frappe.db.set_value(
                "S3 Vault Multipart Upload",
                row.name,
                {"status": "Expired", "completed_on": now_datetime()},
                update_modified=True,
            )
            frappe.db.commit()
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"S3 Vault Multipart Cleanup Failed: {row.name}",
            )
