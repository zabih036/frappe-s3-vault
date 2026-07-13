from __future__ import annotations

import math
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint

from frappe_s3_vault.file_manager_common import (
    basename,
    content_type_for_name,
    full_key,
    get_bucket,
    get_s3_client,
    iso,
    manager_log,
    normalize_relative_path,
    relative_key,
    safe_file_name,
    url_expiry,
)
from frappe_s3_vault.file_manager_permissions import require_access

SINGLE_COPY_LIMIT = 5 * 1024 * 1024 * 1024
MIN_COPY_PART_SIZE = 256 * 1024 * 1024
MAX_COPY_PART_SIZE = 5 * 1024 * 1024 * 1024
MAX_COPY_PARTS = 10_000


def _metadata_args(head: dict) -> dict:
    mapping = {
        "CacheControl": "CacheControl",
        "ContentDisposition": "ContentDisposition",
        "ContentEncoding": "ContentEncoding",
        "ContentLanguage": "ContentLanguage",
        "ContentType": "ContentType",
        "Expires": "Expires",
        "WebsiteRedirectLocation": "WebsiteRedirectLocation",
        "ServerSideEncryption": "ServerSideEncryption",
        "SSEKMSKeyId": "SSEKMSKeyId",
        "BucketKeyEnabled": "BucketKeyEnabled",
        "StorageClass": "StorageClass",
    }
    result = {target: head[source] for source, target in mapping.items() if head.get(source) is not None}
    result["Metadata"] = head.get("Metadata") or {}
    return result


def _copy_version_to_current(bucket, storage_key: str, version_id: str) -> dict:
    client = get_s3_client(bucket)
    source = {"Bucket": bucket.bucket_name, "Key": storage_key, "VersionId": version_id}
    head = client.head_object(
        Bucket=bucket.bucket_name,
        Key=storage_key,
        VersionId=version_id,
    )
    size = int(head.get("ContentLength") or 0)
    if size <= SINGLE_COPY_LIMIT:
        response = client.copy_object(
            Bucket=bucket.bucket_name,
            Key=storage_key,
            CopySource=source,
            MetadataDirective="COPY",
            TaggingDirective="COPY",
        )
        return {
            "VersionId": response.get("VersionId"),
            "ETag": (response.get("CopyObjectResult") or {}).get("ETag"),
            "ContentLength": size,
        }

    required_part_size = math.ceil(size / MAX_COPY_PARTS)
    mib = 1024 * 1024
    copy_part_size = max(MIN_COPY_PART_SIZE, required_part_size)
    copy_part_size = math.ceil(copy_part_size / mib) * mib
    if copy_part_size > MAX_COPY_PART_SIZE:
        frappe.throw(_("This object exceeds the multipart-copy limits supported by S3."))

    create_args = {
        "Bucket": bucket.bucket_name,
        "Key": storage_key,
        **_metadata_args(head),
    }
    created = client.create_multipart_upload(**create_args)
    upload_id = created["UploadId"]
    parts = []
    try:
        total_parts = math.ceil(size / copy_part_size)
        for part_number in range(1, total_parts + 1):
            start = (part_number - 1) * copy_part_size
            end = min(size - 1, start + copy_part_size - 1)
            response = client.upload_part_copy(
                Bucket=bucket.bucket_name,
                Key=storage_key,
                UploadId=upload_id,
                PartNumber=part_number,
                CopySource=source,
                CopySourceRange=f"bytes={start}-{end}",
            )
            parts.append(
                {
                    "PartNumber": part_number,
                    "ETag": response["CopyPartResult"]["ETag"],
                }
            )
        response = client.complete_multipart_upload(
            Bucket=bucket.bucket_name,
            Key=storage_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        return {
            "VersionId": response.get("VersionId"),
            "ETag": response.get("ETag"),
            "ContentLength": size,
        }
    except Exception:
        try:
            client.abort_multipart_upload(
                Bucket=bucket.bucket_name,
                Key=storage_key,
                UploadId=upload_id,
            )
        except Exception:
            pass
        raise


@frappe.whitelist()
def get_versioning_status(connection: str):
    from frappe_s3_vault.file_manager_permissions import accessible_roots
    roots = accessible_roots(connection)
    if not roots:
        frappe.throw(_("You cannot access this S3 connection."), frappe.PermissionError)
    require_access(connection, roots[0], "versions_view")
    bucket = get_bucket(connection, check_permission=False)
    client = get_s3_client(bucket)
    try:
        response = client.get_bucket_versioning(Bucket=bucket.bucket_name)
        return {
            "supported": True,
            "status": response.get("Status") or "Disabled",
            "mfa_delete": response.get("MFADelete"),
        }
    except Exception as exc:
        return {"supported": False, "status": "Unknown", "error": str(exc)}


@frappe.whitelist()
def list_versions(
    connection: str,
    key: str | None = None,
    prefix: str | None = None,
    key_marker: str | None = None,
    version_id_marker: str | None = None,
    max_keys: int = 100,
):
    relative_filter = normalize_relative_path(key or prefix or "", folder=bool(prefix and not key))
    require_access(connection, relative_filter, "versions_view")
    bucket = get_bucket(connection, check_permission=False)
    client = get_s3_client(bucket)
    storage_filter = full_key(bucket, relative_filter, folder=bool(prefix and not key))
    max_keys = max(1, min(cint(max_keys) or 100, 1000))
    params = {
        "Bucket": bucket.bucket_name,
        "Prefix": storage_filter,
        "MaxKeys": max_keys,
    }
    if key_marker:
        params["KeyMarker"] = full_key(bucket, normalize_relative_path(key_marker))
    if version_id_marker:
        params["VersionIdMarker"] = version_id_marker
    response = client.list_object_versions(**params)
    exact_storage_key = full_key(bucket, normalize_relative_path(key)) if key else None
    rows = []
    for version in response.get("Versions", []):
        if exact_storage_key and version.get("Key") != exact_storage_key:
            continue
        rows.append(
            {
                "type": "version",
                "key": relative_key(bucket, version.get("Key") or ""),
                "version_id": version.get("VersionId"),
                "is_latest": bool(version.get("IsLatest")),
                "last_modified": iso(version.get("LastModified")),
                "size": cint(version.get("Size")),
                "etag": str(version.get("ETag") or "").strip('"'),
                "storage_class": version.get("StorageClass"),
            }
        )
    for marker in response.get("DeleteMarkers", []):
        if exact_storage_key and marker.get("Key") != exact_storage_key:
            continue
        rows.append(
            {
                "type": "delete_marker",
                "key": relative_key(bucket, marker.get("Key") or ""),
                "version_id": marker.get("VersionId"),
                "is_latest": bool(marker.get("IsLatest")),
                "last_modified": iso(marker.get("LastModified")),
                "size": 0,
                "etag": None,
                "storage_class": None,
            }
        )
    rows.sort(key=lambda row: row.get("last_modified") or "", reverse=True)
    next_key = response.get("NextKeyMarker")
    return {
        "rows": rows,
        "is_truncated": bool(response.get("IsTruncated")),
        "next_key_marker": relative_key(bucket, next_key) if next_key else None,
        "next_version_id_marker": response.get("NextVersionIdMarker"),
    }


@frappe.whitelist()
def get_version_url(
    connection: str,
    key: str,
    version_id: str,
    disposition: str = "inline",
):
    relative_object_key = normalize_relative_path(key)
    require_access(connection, relative_object_key, "download" if disposition == "attachment" else "preview")
    bucket = get_bucket(connection, check_permission=False)
    client = get_s3_client(bucket)
    storage_key = full_key(bucket, relative_object_key)
    filename = safe_file_name(basename(relative_object_key))
    mode = "attachment" if disposition == "attachment" else "inline"
    expiry = min(url_expiry(), 900)
    url = client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket.bucket_name,
            "Key": storage_key,
            "VersionId": version_id,
            "ResponseContentDisposition": f"{mode}; filename*=UTF-8''{quote(filename)}",
        },
        ExpiresIn=expiry,
    )
    manager_log(
        action="Download" if mode == "attachment" else "Preview",
        bucket=bucket,
        object_key=storage_key,
        user=frappe.session.user,
        message=f"version_id={version_id}",
    )
    return {"url": url, "expires_in": expiry, "filename": filename}


@frappe.whitelist()
def restore_version(connection: str, key: str, version_id: str):
    relative_object_key = normalize_relative_path(key)
    require_access(connection, relative_object_key, "versions_restore")
    bucket = get_bucket(connection, check_permission=False)
    storage_key = full_key(bucket, relative_object_key)
    result = _copy_version_to_current(bucket, storage_key, version_id)
    manager_log(
        action="Copy",
        bucket=bucket,
        source_key=storage_key,
        destination_key=storage_key,
        user=frappe.session.user,
        message=f"Restored version_id={version_id}; new_version_id={result.get('VersionId')}",
    )
    try:
        from frappe_s3_vault.file_manager_index import sync_single_object

        sync_single_object(bucket, storage_key)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Index Update After Version Restore")
    return {
        "key": relative_object_key,
        "restored_version_id": version_id,
        "new_version_id": result.get("VersionId"),
        "size": cint(result.get("ContentLength")),
    }


@frappe.whitelist()
def remove_delete_marker(connection: str, key: str, version_id: str):
    relative_object_key = normalize_relative_path(key)
    require_access(connection, relative_object_key, "versions_restore")
    bucket = get_bucket(connection, check_permission=False)
    client = get_s3_client(bucket)
    storage_key = full_key(bucket, relative_object_key)
    client.delete_object(
        Bucket=bucket.bucket_name,
        Key=storage_key,
        VersionId=version_id,
    )
    manager_log(
        action="Copy",
        bucket=bucket,
        object_key=storage_key,
        user=frappe.session.user,
        message=f"Removed delete marker version_id={version_id}",
    )
    try:
        from frappe_s3_vault.file_manager_index import sync_single_object

        sync_single_object(bucket, storage_key)
    except Exception:
        pass
    return {"key": relative_object_key, "removed_delete_marker": version_id}


@frappe.whitelist()
def permanently_delete_version(
    connection: str,
    key: str,
    version_id: str,
    confirmation: str,
):
    relative_object_key = normalize_relative_path(key)
    require_access(connection, relative_object_key, "versions_delete")
    if confirmation != "PERMANENT DELETE":
        frappe.throw(_("Type PERMANENT DELETE exactly to confirm."))
    bucket = get_bucket(connection, check_permission=False)
    client = get_s3_client(bucket)
    storage_key = full_key(bucket, relative_object_key)
    client.delete_object(
        Bucket=bucket.bucket_name,
        Key=storage_key,
        VersionId=version_id,
    )
    manager_log(
        action="Delete",
        bucket=bucket,
        object_key=storage_key,
        user=frappe.session.user,
        message=f"Permanently deleted version_id={version_id}",
    )
    try:
        from frappe_s3_vault.file_manager_index import sync_single_object

        sync_single_object(bucket, storage_key)
    except Exception:
        pass
    return {"key": relative_object_key, "deleted_version_id": version_id}
