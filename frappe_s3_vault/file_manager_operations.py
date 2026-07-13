from __future__ import annotations

import math
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterable

import frappe
from frappe import _

from frappe_s3_vault.file_manager_common import (
    MAX_BACKGROUND_OBJECTS,
    MAX_ZIP_OBJECTS,
    MAX_ZIP_UNCOMPRESSED_BYTES,
    basename,
    content_type_for_name,
    folder_basename,
    folder_exists,
    full_key,
    get_s3_client,
    linked_records_for_keys,
    manager_log,
    mark_linked_records_deleted,
    normalize_relative_path,
    object_exists,
    parent_prefix,
    relative_key,
    root_prefix,
    safe_file_name,
    safe_folder_name,
    update_linked_records_after_move,
)

SINGLE_COPY_LIMIT = 5 * 1024 * 1024 * 1024
MIN_MULTIPART_PART_SIZE = 100 * 1024 * 1024
MAX_MULTIPART_PARTS = 10_000
DELETE_BATCH_SIZE = 1_000
STREAM_CHUNK_SIZE = 8 * 1024 * 1024

ProgressCallback = Callable[[int, int, str | None], None]


def list_prefix_objects(bucket, relative_prefix: str, max_objects: int = MAX_BACKGROUND_OBJECTS):
    client = get_s3_client(bucket)
    relative_prefix = normalize_relative_path(relative_prefix, folder=True)
    storage_prefix = full_key(bucket, relative_prefix, folder=True)
    paginator = client.get_paginator("list_objects_v2")
    count = 0

    for page in paginator.paginate(Bucket=bucket.bucket_name, Prefix=storage_prefix):
        for row in page.get("Contents", []):
            count += 1
            if max_objects and count > max_objects:
                frappe.throw(
                    _("This operation exceeds the safety limit of {0} objects.").format(
                        max_objects
                    )
                )
            yield {
                "Key": row.get("Key"),
                "Size": int(row.get("Size") or 0),
                "ETag": str(row.get("ETag") or "").strip('"'),
                "LastModified": row.get("LastModified"),
                "StorageClass": row.get("StorageClass"),
            }


def collect_prefix_objects(bucket, relative_prefix: str, max_objects: int = MAX_BACKGROUND_OBJECTS):
    return list(list_prefix_objects(bucket, relative_prefix, max_objects=max_objects))


def get_folder_summary(bucket, relative_prefix: str, max_objects: int):
    relative_prefix = normalize_relative_path(relative_prefix, folder=True)
    storage_prefix = full_key(bucket, relative_prefix, folder=True)
    client = get_s3_client(bucket)
    paginator = client.get_paginator("list_objects_v2")

    total_objects = 0
    total_bytes = 0
    sampled_keys: list[str] = []
    truncated = False

    for page in paginator.paginate(Bucket=bucket.bucket_name, Prefix=storage_prefix):
        contents = page.get("Contents", [])
        for row_index, row in enumerate(contents):
            total_objects += 1
            total_bytes += int(row.get("Size") or 0)
            sampled_keys.append(row.get("Key"))
            if total_objects >= max_objects:
                truncated = row_index < len(contents) - 1 or bool(page.get("IsTruncated"))
                break
        if total_objects >= max_objects:
            break

    linked = linked_records_for_keys(bucket, sampled_keys)
    linked_count = sum(len(rows) for rows in linked.values())

    return {
        "prefix": relative_prefix,
        "object_count": total_objects,
        "total_bytes": total_bytes,
        "linked_count": linked_count,
        "truncated": truncated,
        "limit": max_objects,
    }


def _metadata_args(head: dict) -> dict:
    args = {}
    for source, destination in {
        "ContentType": "ContentType",
        "CacheControl": "CacheControl",
        "ContentDisposition": "ContentDisposition",
        "ContentEncoding": "ContentEncoding",
        "ContentLanguage": "ContentLanguage",
        "Expires": "Expires",
        "Metadata": "Metadata",
        "ServerSideEncryption": "ServerSideEncryption",
        "SSEKMSKeyId": "SSEKMSKeyId",
        "StorageClass": "StorageClass",
    }.items():
        value = head.get(source)
        if value not in (None, "", {}):
            args[destination] = value
    return args


def _copy_tags(client, bucket_name: str, source_key: str, destination_key: str):
    try:
        response = client.get_object_tagging(Bucket=bucket_name, Key=source_key)
        tags = response.get("TagSet") or []
        if tags:
            client.put_object_tagging(
                Bucket=bucket_name,
                Key=destination_key,
                Tagging={"TagSet": tags},
            )
    except Exception:
        # Some S3-compatible providers do not implement object tags.
        pass


def copy_storage_object(
    bucket,
    source_key: str,
    destination_key: str,
    progress: ProgressCallback | None = None,
) -> dict:
    client = get_s3_client(bucket)
    head = client.head_object(Bucket=bucket.bucket_name, Key=source_key)
    size = int(head.get("ContentLength") or 0)

    if size <= SINGLE_COPY_LIMIT:
        kwargs = {
            "Bucket": bucket.bucket_name,
            "CopySource": {"Bucket": bucket.bucket_name, "Key": source_key},
            "Key": destination_key,
            "MetadataDirective": "COPY",
            "TaggingDirective": "COPY",
        }
        if head.get("StorageClass"):
            kwargs["StorageClass"] = head.get("StorageClass")
        try:
            response = client.copy_object(**kwargs)
        except Exception:
            # Retry without TaggingDirective for providers with partial S3 compatibility.
            kwargs.pop("TaggingDirective", None)
            response = client.copy_object(**kwargs)
        if progress:
            progress(1, size, None)
        destination_head = client.head_object(
            Bucket=bucket.bucket_name,
            Key=destination_key,
        )
        destination_head["VersionId"] = response.get("VersionId") or destination_head.get(
            "VersionId"
        )
        return destination_head

    part_size = max(
        MIN_MULTIPART_PART_SIZE,
        math.ceil(size / MAX_MULTIPART_PARTS),
    )
    # Keep ranges on MiB boundaries for broad provider compatibility.
    mib = 1024 * 1024
    part_size = math.ceil(part_size / mib) * mib

    create_args = {
        "Bucket": bucket.bucket_name,
        "Key": destination_key,
        **_metadata_args(head),
    }
    multipart = client.create_multipart_upload(**create_args)
    upload_id = multipart["UploadId"]
    parts = []

    try:
        part_number = 1
        start = 0
        while start < size:
            end = min(start + part_size - 1, size - 1)
            result = client.upload_part_copy(
                Bucket=bucket.bucket_name,
                Key=destination_key,
                PartNumber=part_number,
                UploadId=upload_id,
                CopySource={"Bucket": bucket.bucket_name, "Key": source_key},
                CopySourceRange=f"bytes={start}-{end}",
            )
            parts.append(
                {
                    "PartNumber": part_number,
                    "ETag": result["CopyPartResult"]["ETag"],
                }
            )
            if progress:
                progress(0, end - start + 1, None)
            part_number += 1
            start = end + 1

        complete = client.complete_multipart_upload(
            Bucket=bucket.bucket_name,
            Key=destination_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        _copy_tags(client, bucket.bucket_name, source_key, destination_key)
        if progress:
            progress(1, 0, None)
        destination_head = client.head_object(
            Bucket=bucket.bucket_name,
            Key=destination_key,
        )
        destination_head["VersionId"] = complete.get("VersionId") or destination_head.get(
            "VersionId"
        )
        return destination_head
    except Exception:
        try:
            client.abort_multipart_upload(
                Bucket=bucket.bucket_name,
                Key=destination_key,
                UploadId=upload_id,
            )
        except Exception:
            pass
        raise


def delete_storage_keys(bucket, keys: Iterable[str]) -> list[dict]:
    client = get_s3_client(bucket)
    key_list = list(dict.fromkeys(key for key in keys if key))
    errors: list[dict] = []

    for index in range(0, len(key_list), DELETE_BATCH_SIZE):
        batch = key_list[index : index + DELETE_BATCH_SIZE]
        response = client.delete_objects(
            Bucket=bucket.bucket_name,
            Delete={
                "Objects": [{"Key": key} for key in batch],
                "Quiet": False,
            },
        )
        errors.extend(response.get("Errors") or [])

    return errors


def unique_file_storage_key(bucket, desired_storage_key: str) -> str:
    client = get_s3_client(bucket)
    if not object_exists(client, bucket.bucket_name, desired_storage_key):
        return desired_storage_key

    directory, filename = os.path.split(desired_storage_key)
    stem, extension = os.path.splitext(filename)
    for number in range(1, 10_000):
        candidate_name = f"{stem} ({number}){extension}"
        candidate = f"{directory}/{candidate_name}" if directory else candidate_name
        if not object_exists(client, bucket.bucket_name, candidate):
            return candidate

    frappe.throw(_("Could not generate a unique destination file name."))


def unique_folder_storage_prefix(bucket, desired_storage_prefix: str) -> str:
    client = get_s3_client(bucket)
    desired_storage_prefix = desired_storage_prefix.rstrip("/") + "/"
    if not folder_exists(client, bucket.bucket_name, desired_storage_prefix):
        return desired_storage_prefix

    parent, folder_name = os.path.split(desired_storage_prefix.rstrip("/"))
    for number in range(1, 10_000):
        candidate_name = f"{folder_name} ({number})"
        candidate = f"{parent}/{candidate_name}/" if parent else f"{candidate_name}/"
        if not folder_exists(client, bucket.bucket_name, candidate):
            return candidate

    frappe.throw(_("Could not generate a unique destination folder name."))


def resolve_file_destination(
    bucket,
    destination_relative_key: str,
    conflict_strategy: str,
) -> str:
    destination_storage_key = full_key(bucket, destination_relative_key)
    client = get_s3_client(bucket)
    exists = object_exists(client, bucket.bucket_name, destination_storage_key)

    if not exists:
        return destination_storage_key
    if conflict_strategy == "replace":
        linked = linked_records_for_keys(bucket, [destination_storage_key]).get(
            destination_storage_key, []
        )
        if linked:
            frappe.throw(
                _(
                    "The destination object is a managed Frappe attachment and cannot be replaced from the file manager."
                )
            )
        return destination_storage_key
    if conflict_strategy == "keep_both":
        return unique_file_storage_key(bucket, destination_storage_key)
    frappe.throw(_("A destination object already exists: {0}").format(destination_relative_key))


def resolve_folder_destination(
    bucket,
    destination_relative_prefix: str,
    conflict_strategy: str,
) -> str:
    destination_storage_prefix = full_key(bucket, destination_relative_prefix, folder=True)
    client = get_s3_client(bucket)
    exists = folder_exists(client, bucket.bucket_name, destination_storage_prefix)

    if not exists:
        return destination_storage_prefix
    if conflict_strategy == "keep_both":
        return unique_folder_storage_prefix(bucket, destination_storage_prefix)
    if conflict_strategy == "replace":
        frappe.throw(
            _("Replace is not allowed for folders. Choose Stop if exists or Keep both.")
        )
    frappe.throw(
        _("A destination folder already exists: {0}").format(destination_relative_prefix)
    )


def rename_or_transfer_file(
    *,
    bucket,
    source_relative_key: str,
    destination_parent: str,
    new_name: str | None,
    mode: str,
    conflict_strategy: str,
    update_linked_record: bool,
    user: str,
) -> dict:
    source_relative_key = normalize_relative_path(source_relative_key)
    destination_parent = normalize_relative_path(destination_parent, folder=bool(destination_parent))
    filename = safe_file_name(new_name or basename(source_relative_key))
    destination_relative_key = normalize_relative_path(f"{destination_parent}{filename}")

    source_storage_key = full_key(bucket, source_relative_key)
    client = get_s3_client(bucket)
    if not object_exists(client, bucket.bucket_name, source_storage_key):
        frappe.throw(_("Source object does not exist: {0}").format(source_relative_key))

    desired_storage_key = full_key(bucket, destination_relative_key)
    if desired_storage_key == source_storage_key:
        frappe.throw(_("Source and destination are the same."))

    destination_existed = object_exists(
        client, bucket.bucket_name, desired_storage_key
    )
    destination_storage_key = resolve_file_destination(
        bucket,
        destination_relative_key,
        conflict_strategy,
    )
    if destination_storage_key != desired_storage_key:
        destination_existed = False

    metadata = copy_storage_object(bucket, source_storage_key, destination_storage_key)

    if mode == "move":
        errors = delete_storage_keys(bucket, [source_storage_key])
        if errors:
            # Only remove the copied destination when it was newly created. A
            # replacement destination cannot be restored safely without version
            # recovery, so retain it and also retain the source.
            if not destination_existed:
                try:
                    delete_storage_keys(bucket, [destination_storage_key])
                except Exception:
                    pass
            frappe.throw(
                _(
                    "The destination was copied, but the source object could not be deleted. Review both keys before retrying."
                )
            )

        if update_linked_record:
            update_linked_records_after_move(
                bucket,
                {source_storage_key: destination_storage_key},
                {destination_storage_key: metadata},
            )

    manager_log(
        action="Move" if mode == "move" else "Copy",
        bucket=bucket,
        source_key=source_storage_key,
        destination_key=destination_storage_key,
        user=user,
        message="S3 File Manager file operation",
    )

    return {
        "source_key": source_relative_key,
        "destination_key": relative_key(bucket, destination_storage_key),
        "name": basename(relative_key(bucket, destination_storage_key)),
        "size": int(metadata.get("ContentLength") or 0),
        "content_type": metadata.get("ContentType")
        or content_type_for_name(destination_storage_key),
        "etag": str(metadata.get("ETag") or "").strip('"'),
        "version_id": metadata.get("VersionId"),
    }


def delete_file(
    *,
    bucket,
    source_relative_key: str,
    allow_linked_delete: bool,
    user: str,
) -> dict:
    source_relative_key = normalize_relative_path(source_relative_key)
    storage_key = full_key(bucket, source_relative_key)
    client = get_s3_client(bucket)
    if not object_exists(client, bucket.bucket_name, storage_key):
        frappe.throw(_("Object does not exist: {0}").format(source_relative_key))

    linked = linked_records_for_keys(bucket, [storage_key]).get(storage_key, [])
    if linked and not allow_linked_delete:
        frappe.throw(
            _(
                "This object is linked to a Frappe attachment. Enable linked-file deletion only after confirming the attachment may stop working."
            )
        )

    errors = delete_storage_keys(bucket, [storage_key])
    if errors:
        frappe.throw(_("S3 returned an error while deleting the object."))

    if linked:
        mark_linked_records_deleted(bucket, [storage_key])

    manager_log(
        action="Delete",
        bucket=bucket,
        object_key=storage_key,
        user=user,
        message=f"linked_records={len(linked)}",
    )
    return {"deleted": True, "key": source_relative_key, "linked_records": len(linked)}


def build_item_objects(bucket, item: dict) -> list[dict]:
    if item["type"] == "file":
        storage_key = full_key(bucket, item["key"])
        head = get_s3_client(bucket).head_object(
            Bucket=bucket.bucket_name,
            Key=storage_key,
        )
        return [
            {
                "Key": storage_key,
                "Size": int(head.get("ContentLength") or 0),
                "relative_source": item["key"],
                "selected_root": item["key"],
                "selected_type": "file",
            }
        ]

    rows = collect_prefix_objects(bucket, item["key"])
    storage_prefix = full_key(bucket, item["key"], folder=True)
    if not rows and not object_exists(
        get_s3_client(bucket), bucket.bucket_name, storage_prefix
    ):
        frappe.throw(_("Source folder does not exist: {0}").format(item["key"]))
    return [
        {
            **row,
            "relative_source": relative_key(bucket, row["Key"]),
            "selected_root": item["key"],
            "selected_type": "folder",
            "root_suffix": row["Key"][len(storage_prefix) :],
        }
        for row in rows
    ]


def prepare_transfer_plan(
    *,
    bucket,
    items: list[dict],
    destination_parent: str,
    conflict_strategy: str,
    rename_to: str | None = None,
) -> list[dict]:
    destination_parent = normalize_relative_path(destination_parent, folder=bool(destination_parent))
    if conflict_strategy == "replace" and len(items) > 1:
        frappe.throw(
            _(
                "Replace is not allowed for multi-item background operations. Use Stop or Keep both."
            )
        )
    plan: list[dict] = []
    client = get_s3_client(bucket)

    for item in items:
        source_objects = build_item_objects(bucket, item)

        if item["type"] == "file":
            destination_name = safe_file_name(rename_to or item["name"])
            desired_relative_key = normalize_relative_path(
                f"{destination_parent}{destination_name}"
            )
            destination_storage_key = resolve_file_destination(
                bucket,
                desired_relative_key,
                conflict_strategy,
            )
            plan.append(
                {
                    **source_objects[0],
                    "destination_key": destination_storage_key,
                    "relative_destination": relative_key(bucket, destination_storage_key),
                }
            )
            continue

        destination_folder_name = safe_folder_name(rename_to or item["name"])
        desired_relative_prefix = normalize_relative_path(
            f"{destination_parent}{destination_folder_name}",
            folder=True,
        )
        destination_storage_prefix = resolve_folder_destination(
            bucket,
            desired_relative_prefix,
            conflict_strategy,
        )
        source_storage_prefix = full_key(bucket, item["key"], folder=True)

        if destination_storage_prefix.startswith(source_storage_prefix):
            frappe.throw(_("A folder cannot be copied or moved inside itself."))

        if not source_objects:
            # Preserve a genuinely empty folder marker.
            source_marker_exists = object_exists(client, bucket.bucket_name, source_storage_prefix)
            if source_marker_exists:
                source_objects = [
                    {
                        "Key": source_storage_prefix,
                        "Size": 0,
                        "relative_source": item["key"],
                        "selected_root": item["key"],
                        "selected_type": "folder",
                        "root_suffix": "",
                    }
                ]

        for row in source_objects:
            suffix = row.get("root_suffix")
            if suffix is None:
                suffix = row["Key"][len(source_storage_prefix) :]
            destination_storage_key = f"{destination_storage_prefix}{suffix}"
            plan.append(
                {
                    **row,
                    "destination_key": destination_storage_key,
                    "relative_destination": relative_key(bucket, destination_storage_key),
                }
            )

    if len(plan) > MAX_BACKGROUND_OBJECTS:
        frappe.throw(
            _("This operation exceeds the safety limit of {0} objects.").format(
                MAX_BACKGROUND_OBJECTS
            )
        )

    source_keys = {row["Key"] for row in plan}
    destination_keys = [row["destination_key"] for row in plan]
    if len(destination_keys) != len(set(destination_keys)):
        frappe.throw(_("The selected items produce duplicate destination keys."))
    if any(key in source_keys for key in destination_keys):
        frappe.throw(_("One or more destination keys overlap selected source objects."))

    return plan


def execute_transfer_plan(
    *,
    bucket,
    plan: list[dict],
    mode: str,
    update_linked_records: bool,
    user: str,
    progress: ProgressCallback | None = None,
) -> dict:
    copied_keys: list[str] = []
    metadata_by_destination: dict[str, dict] = {}
    key_map = {row["Key"]: row["destination_key"] for row in plan}

    try:
        for row in plan:
            metadata = copy_storage_object(
                bucket,
                row["Key"],
                row["destination_key"],
                progress=progress,
            )
            copied_keys.append(row["destination_key"])
            metadata_by_destination[row["destination_key"]] = metadata
    except Exception:
        rollback_errors = []
        try:
            rollback_errors = delete_storage_keys(bucket, copied_keys)
        except Exception:
            rollback_errors = [{"Message": frappe.get_traceback()}]
        if rollback_errors:
            frappe.log_error(
                str(rollback_errors),
                "S3 File Manager Rollback Failed",
            )
        raise

    if mode == "move":
        errors = delete_storage_keys(bucket, [row["Key"] for row in plan])
        # All destinations are already present. Update managed attachment records
        # to the new keys even if a few source deletes fail; leftover sources are
        # safer than attachment records pointing to keys that were deleted.
        if update_linked_records:
            update_linked_records_after_move(bucket, key_map, metadata_by_destination)
        if errors:
            frappe.throw(
                _(
                    "All destination objects were copied, but some source objects could not be deleted. Managed records now point to the destination; review leftover source keys before retrying."
                )
            )

    source_description = plan[0]["Key"] if len(plan) == 1 else f"{len(plan)} objects"
    destination_description = (
        plan[0]["destination_key"] if len(plan) == 1 else f"{len(plan)} objects"
    )
    manager_log(
        action="Move" if mode == "move" else "Copy",
        bucket=bucket,
        source_key=source_description,
        destination_key=destination_description,
        user=user,
        message=f"objects={len(plan)}",
    )

    return {
        "objects": len(plan),
        "bytes": sum(int(row.get("Size") or 0) for row in plan),
        "key_map": key_map,
    }


def prepare_delete_plan(bucket, items: list[dict]) -> list[dict]:
    plan: list[dict] = []
    seen = set()
    for item in items:
        for row in build_item_objects(bucket, item):
            if row["Key"] in seen:
                continue
            seen.add(row["Key"])
            plan.append(row)

    if len(plan) > MAX_BACKGROUND_OBJECTS:
        frappe.throw(
            _("This operation exceeds the safety limit of {0} objects.").format(
                MAX_BACKGROUND_OBJECTS
            )
        )
    return plan


def execute_delete_plan(
    *,
    bucket,
    plan: list[dict],
    allow_linked_delete: bool,
    user: str,
    progress: ProgressCallback | None = None,
) -> dict:
    keys = [row["Key"] for row in plan]
    linked = linked_records_for_keys(bucket, keys)
    linked_count = sum(len(rows) for rows in linked.values())

    if linked_count and not allow_linked_delete:
        frappe.throw(
            _(
                "The selection contains {0} linked Frappe attachment record(s). Confirm linked-file deletion before continuing."
            ).format(linked_count)
        )

    errors = delete_storage_keys(bucket, keys)
    failed_keys = {str(row.get("Key")) for row in errors if row.get("Key")}
    deleted_keys = [key for key in keys if key not in failed_keys]

    if linked_count and deleted_keys:
        mark_linked_records_deleted(bucket, deleted_keys)

    if progress:
        for row in plan:
            if row["Key"] not in failed_keys:
                progress(1, int(row.get("Size") or 0), None)

    if errors:
        frappe.throw(_("S3 returned errors for one or more delete requests: {0}").format(errors))

    manager_log(
        action="Delete",
        bucket=bucket,
        object_key=keys[0] if len(keys) == 1 else f"{len(keys)} objects",
        user=user,
        message=f"objects={len(keys)}; linked_records={linked_count}",
    )
    return {
        "objects": len(keys),
        "bytes": sum(int(row.get("Size") or 0) for row in plan),
        "linked_records": linked_count,
    }


def _safe_archive_path(value: str) -> str:
    value = str(value or "").replace("\\", "/").lstrip("/")
    parts = [part for part in value.split("/") if part not in {"", ".", ".."}]
    return "/".join(parts)


def prepare_zip_plan(bucket, items: list[dict]) -> list[dict]:
    plan: list[dict] = []
    seen = set()

    for item in items:
        if item["type"] == "file":
            rows = build_item_objects(bucket, item)
            archive_name = _safe_archive_path(item["name"])
            row = rows[0]
            identity = (row["Key"], archive_name)
            if identity not in seen:
                seen.add(identity)
                plan.append({**row, "archive_name": archive_name})
            continue

        rows = build_item_objects(bucket, item)
        root_name = safe_folder_name(item["name"])
        source_storage_prefix = full_key(bucket, item["key"], folder=True)
        for row in rows:
            suffix = row["Key"][len(source_storage_prefix) :]
            if not suffix or row["Key"].endswith("/"):
                continue
            archive_name = _safe_archive_path(f"{root_name}/{suffix}")
            identity = (row["Key"], archive_name)
            if identity in seen:
                continue
            seen.add(identity)
            plan.append({**row, "archive_name": archive_name})

    total_bytes = sum(int(row.get("Size") or 0) for row in plan)
    if len(plan) > MAX_ZIP_OBJECTS:
        frappe.throw(
            _("ZIP creation is limited to {0} files per operation.").format(MAX_ZIP_OBJECTS)
        )
    if total_bytes > MAX_ZIP_UNCOMPRESSED_BYTES:
        frappe.throw(
            _("ZIP creation is limited to {0} bytes of uncompressed data.").format(
                MAX_ZIP_UNCOMPRESSED_BYTES
            )
        )
    return plan


def create_zip_archive(
    *,
    bucket,
    plan: list[dict],
    archive_storage_key: str,
    progress: ProgressCallback | None = None,
) -> dict:
    client = get_s3_client(bucket)
    total_bytes = sum(int(row.get("Size") or 0) for row in plan)
    temp_dir = tempfile.gettempdir()
    free_bytes = shutil.disk_usage(temp_dir).free
    required_bytes = max(256 * 1024 * 1024, int(total_bytes * 1.15))
    if free_bytes < required_bytes:
        frappe.throw(
            _(
                "Not enough temporary disk space to create this archive. Required approximately {0} bytes."
            ).format(required_bytes)
        )

    temp_file = tempfile.NamedTemporaryFile(
        prefix="s3-vault-",
        suffix=".zip",
        delete=False,
    )
    temp_path = temp_file.name
    temp_file.close()

    try:
        with zipfile.ZipFile(
            temp_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            for row in plan:
                response = client.get_object(
                    Bucket=bucket.bucket_name,
                    Key=row["Key"],
                )
                body = response["Body"]
                try:
                    with archive.open(
                        row["archive_name"], mode="w", force_zip64=True
                    ) as target:
                        while True:
                            chunk = body.read(STREAM_CHUNK_SIZE)
                            if not chunk:
                                break
                            target.write(chunk)
                            if progress:
                                progress(0, len(chunk), row["archive_name"])
                finally:
                    body.close()
                if progress:
                    progress(1, 0, row["archive_name"])

        client.upload_file(
            Filename=temp_path,
            Bucket=bucket.bucket_name,
            Key=archive_storage_key,
            ExtraArgs={"ContentType": "application/zip"},
        )
        head = client.head_object(
            Bucket=bucket.bucket_name,
            Key=archive_storage_key,
        )
        return {
            "storage_key": archive_storage_key,
            "archive_size": int(head.get("ContentLength") or 0),
            "source_files": len(plan),
            "source_bytes": total_bytes,
            "etag": str(head.get("ETag") or "").strip('"'),
        }
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
