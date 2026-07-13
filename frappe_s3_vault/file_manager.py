from __future__ import annotations

import mimetypes
import os
import re
from urllib.parse import quote

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


def _require_system_manager() -> None:
    if "System Manager" not in frappe.get_roles():
        frappe.throw(
            _("Only a System Manager can use the S3 File Manager."),
            frappe.PermissionError,
        )


def _get_settings():
    try:
        return frappe.get_single("S3 Vault Settings")
    except Exception:
        return None


def _get_bucket(connection: str):
    _require_system_manager()

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


def _normalize_relative_path(value: str | None, folder: bool = False) -> str:
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


def _root_prefix(bucket) -> str:
    base_prefix = _normalize_relative_path(bucket.base_prefix or "")
    return f"{base_prefix}/" if base_prefix else ""


def _full_key(bucket, relative_key: str | None = "", folder: bool = False) -> str:
    relative_key = _normalize_relative_path(relative_key, folder=folder)
    return f"{_root_prefix(bucket)}{relative_key}"


def _relative_key(bucket, full_key: str) -> str:
    root = _root_prefix(bucket)

    if root and not full_key.startswith(root):
        frappe.throw(_("The requested object is outside the configured base prefix."))

    return full_key[len(root) :] if root else full_key


def _safe_file_name(filename: str | None) -> str:
    filename = str(filename or "").replace("\\", "/")
    filename = os.path.basename(filename).strip()

    if not filename or filename in {".", ".."} or "\x00" in filename:
        frappe.throw(_("Invalid file name."))

    if len(filename.encode("utf-8")) > 255:
        frappe.throw(_("File name is too long."))

    return filename


def _safe_folder_name(folder_name: str | None) -> str:
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


def _get_error_code(exc: ClientError) -> str:
    try:
        return str(exc.response.get("Error", {}).get("Code", ""))
    except Exception:
        return ""


def _object_exists(client, bucket_name: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket_name, Key=key)
        return True
    except ClientError as exc:
        if _get_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _folder_exists(client, bucket_name: str, prefix: str) -> bool:
    result = client.list_objects_v2(
        Bucket=bucket_name,
        Prefix=prefix,
        MaxKeys=1,
    )
    return bool(result.get("KeyCount") or result.get("Contents"))


def _url_expiry() -> int:
    settings = _get_settings()
    configured = cint(getattr(settings, "default_url_expiry_seconds", 0)) if settings else 0
    return max(60, min(configured or DEFAULT_URL_EXPIRY_SECONDS, 3600))


def _blocked_extensions() -> set[str]:
    settings = _get_settings()
    value = getattr(settings, "global_blocked_extensions", "") if settings else ""
    return {
        item.strip().lower().lstrip(".")
        for item in re.split(r"[\n,;| ]+", str(value or ""))
        if item.strip()
    }


def _iso(value):
    return value.isoformat() if value else None


@frappe.whitelist()
def get_connections():
    """Return enabled S3 connections without exposing credentials."""
    _require_system_manager()

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

    settings = _get_settings()
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
    """List only the immediate folders and files below a relative prefix."""
    bucket = _get_bucket(connection)
    client = s3_client(bucket)

    relative_prefix = _normalize_relative_path(prefix, folder=bool(prefix))
    full_prefix = _full_key(bucket, relative_prefix, folder=bool(relative_prefix))

    page_size = max(1, min(cint(page_size) or DEFAULT_LIST_PAGE_SIZE, MAX_LIST_PAGE_SIZE))

    params = {
        "Bucket": bucket.bucket_name,
        "Prefix": full_prefix,
        "Delimiter": "/",
        "MaxKeys": page_size,
    }

    if continuation_token:
        params["ContinuationToken"] = continuation_token

    response = client.list_objects_v2(**params)

    folders = []
    for item in response.get("CommonPrefixes", []):
        full_folder_key = item.get("Prefix") or ""
        relative_folder_key = _relative_key(bucket, full_folder_key)
        folder_name = relative_folder_key.rstrip("/").split("/")[-1]

        if folder_name:
            folders.append(
                {
                    "name": folder_name,
                    "key": relative_folder_key,
                    "type": "folder",
                }
            )

    files = []
    for item in response.get("Contents", []):
        full_object_key = item.get("Key") or ""

        # Skip the marker representing the currently opened folder.
        if not full_object_key or full_object_key == full_prefix or full_object_key.endswith("/"):
            continue

        relative_object_key = _relative_key(bucket, full_object_key)
        filename = relative_object_key.rsplit("/", 1)[-1]
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        files.append(
            {
                "name": filename,
                "key": relative_object_key,
                "type": "file",
                "size": cint(item.get("Size")),
                "last_modified": _iso(item.get("LastModified")),
                "etag": str(item.get("ETag") or "").strip('"'),
                "storage_class": item.get("StorageClass"),
                "content_type": content_type,
            }
        )

    folders.sort(key=lambda row: row["name"].lower())
    files.sort(key=lambda row: row["name"].lower())

    return {
        "connection": bucket.name,
        "bucket_title": bucket.bucket_title,
        "bucket_name": bucket.bucket_name,
        "provider_type": bucket.provider_type,
        "base_prefix": _root_prefix(bucket),
        "prefix": relative_prefix,
        "folders": folders,
        "files": files,
        "is_truncated": bool(response.get("IsTruncated")),
        "next_token": response.get("NextContinuationToken"),
        "key_count": cint(response.get("KeyCount")),
    }


@frappe.whitelist()
def create_folder(connection: str, prefix: str | None, folder_name: str):
    """Create an S3 folder marker below the configured virtual root."""
    bucket = _get_bucket(connection)
    client = s3_client(bucket)

    parent_prefix = _normalize_relative_path(prefix, folder=bool(prefix))
    folder_name = _safe_folder_name(folder_name)
    relative_folder = f"{parent_prefix}{folder_name}/"
    full_folder = _full_key(bucket, relative_folder, folder=True)

    if _folder_exists(client, bucket.bucket_name, full_folder):
        frappe.throw(_("Folder {0} already exists.").format(folder_name))

    client.put_object(
        Bucket=bucket.bucket_name,
        Key=full_folder,
        Body=b"",
        ContentType="application/x-directory",
    )

    write_log(
        action="Upload",
        status="Success",
        bucket_name=bucket.bucket_name,
        object_key=full_folder,
        error_message="S3 File Manager created a folder marker.",
    )

    return {
        "name": folder_name,
        "key": relative_folder,
    }


@frappe.whitelist()
def get_object_url(
    connection: str,
    key: str,
    disposition: str = "inline",
):
    """Create a short-lived preview or download URL for one object."""
    bucket = _get_bucket(connection)
    client = s3_client(bucket)

    relative_key = _normalize_relative_path(key)
    if not relative_key:
        frappe.throw(_("Object key is required."))

    full_key = _full_key(bucket, relative_key)
    metadata = client.head_object(Bucket=bucket.bucket_name, Key=full_key)

    mode = "attachment" if disposition == "attachment" else "inline"
    filename = _safe_file_name(os.path.basename(relative_key))
    response_disposition = f"{mode}; filename*=UTF-8''{quote(filename)}"
    expiry = _url_expiry()

    url = client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket.bucket_name,
            "Key": full_key,
            "ResponseContentDisposition": response_disposition,
        },
        ExpiresIn=expiry,
    )

    write_log(
        action="Download" if mode == "attachment" else "Preview",
        status="Success",
        bucket_name=bucket.bucket_name,
        object_key=full_key,
    )

    return {
        "url": url,
        "expires_in": expiry,
        "name": filename,
        "key": relative_key,
        "content_type": metadata.get("ContentType")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream",
        "size": cint(metadata.get("ContentLength")),
        "etag": str(metadata.get("ETag") or "").strip('"'),
        "last_modified": _iso(metadata.get("LastModified")),
    }


@frappe.whitelist()
def create_upload_session(
    connection: str,
    prefix: str | None,
    filename: str,
    content_type: str | None = None,
    file_size: int = 0,
    overwrite: int = 0,
):
    """Return a presigned PUT URL. The browser uploads directly to S3."""
    bucket = _get_bucket(connection)
    client = s3_client(bucket)

    parent_prefix = _normalize_relative_path(prefix, folder=bool(prefix))
    filename = _safe_file_name(filename)
    file_size = cint(file_size)

    if file_size <= 0:
        frappe.throw(_("The selected file is empty."))

    if file_size > MAX_DIRECT_UPLOAD_BYTES:
        frappe.throw(
            _("Part 1 direct upload is limited to 500 MB per file. Multipart upload will be added later.")
        )

    extension = os.path.splitext(filename)[1].lower().lstrip(".")
    if extension and extension in _blocked_extensions():
        frappe.throw(_("File type .{0} is blocked by S3 Vault Settings.").format(extension))

    relative_key = f"{parent_prefix}{filename}"
    full_key = _full_key(bucket, relative_key)

    if not cint(overwrite) and _object_exists(client, bucket.bucket_name, full_key):
        frappe.throw(
            _("A file named {0} already exists in this folder.").format(filename)
        )

    content_type = (
        str(content_type or "").strip()
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    expiry = min(_url_expiry(), 900)

    upload_url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket.bucket_name,
            "Key": full_key,
            "ContentType": content_type,
        },
        ExpiresIn=expiry,
    )

    return {
        "upload_url": upload_url,
        "method": "PUT",
        "headers": {
            "Content-Type": content_type,
        },
        "expires_in": expiry,
        "name": filename,
        "key": relative_key,
        "expected_size": file_size,
        "content_type": content_type,
    }


@frappe.whitelist()
def complete_upload(
    connection: str,
    key: str,
    expected_size: int = 0,
):
    """Verify a direct upload and write its audit log."""
    bucket = _get_bucket(connection)
    client = s3_client(bucket)

    relative_key = _normalize_relative_path(key)
    if not relative_key:
        frappe.throw(_("Object key is required."))

    full_key = _full_key(bucket, relative_key)
    metadata = client.head_object(Bucket=bucket.bucket_name, Key=full_key)

    actual_size = cint(metadata.get("ContentLength"))
    expected_size = cint(expected_size)

    if expected_size and actual_size != expected_size:
        frappe.throw(
            _(
                "Upload size verification failed. Expected {0} bytes but S3 contains {1} bytes."
            ).format(expected_size, actual_size)
        )

    write_log(
        action="Upload",
        status="Success",
        bucket_name=bucket.bucket_name,
        object_key=full_key,
    )

    filename = relative_key.rsplit("/", 1)[-1]

    return {
        "name": filename,
        "key": relative_key,
        "size": actual_size,
        "content_type": metadata.get("ContentType")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream",
        "etag": str(metadata.get("ETag") or "").strip('"'),
        "last_modified": _iso(metadata.get("LastModified")),
    }
