from __future__ import annotations

import hashlib
import os
import time

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from frappe_s3_vault.file_manager_common import (
    TEMP_ARCHIVE_RETENTION_HOURS,
    format_bytes,
    full_key,
    get_bucket,
    get_s3_client,
    manager_log,
    normalize_relative_path,
    operation_as_dict,
    parse_json,
    relative_key,
)
from frappe_s3_vault.file_manager_operations import (
    create_zip_archive,
    delete_storage_keys,
    execute_delete_plan,
    execute_transfer_plan,
    prepare_delete_plan,
    prepare_transfer_plan,
    prepare_zip_plan,
)


def _save_operation(doc, commit: bool = True, **values):
    for fieldname, value in values.items():
        if doc.meta.has_field(fieldname):
            doc.set(fieldname, value)
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    if commit:
        frappe.db.commit()
    _publish(doc)


def _publish(doc):
    try:
        frappe.publish_realtime(
            "s3_vault_operation_progress",
            operation_as_dict(doc),
            user=doc.started_by,
            after_commit=False,
        )
    except Exception:
        pass


def _progress_callback(doc):
    processed_objects = cint(doc.processed_objects)
    processed_bytes = int(doc.processed_size or 0)
    last_saved_objects = processed_objects
    last_saved_bytes = processed_bytes
    last_saved_at = time.monotonic()

    def update(object_delta: int, byte_delta: int, message: str | None = None):
        nonlocal processed_objects, processed_bytes
        nonlocal last_saved_objects, last_saved_bytes, last_saved_at

        processed_objects += cint(object_delta)
        processed_bytes += int(byte_delta or 0)
        now_mono = time.monotonic()

        # Avoid a database commit for every 8 MB ZIP stream chunk. Save when an
        # object finishes, after 64 MB, or at least every two seconds.
        should_save = bool(object_delta)
        should_save = should_save or processed_bytes - last_saved_bytes >= 64 * 1024 * 1024
        should_save = should_save or now_mono - last_saved_at >= 2
        if not should_save:
            return

        total_objects = max(cint(doc.total_objects), 1)
        object_progress = min(100, (processed_objects / total_objects) * 100)
        total_bytes = int(doc.total_size or 0)
        byte_progress = (processed_bytes / total_bytes) * 100 if total_bytes else 0
        progress = min(99, max(object_progress, byte_progress))

        _save_operation(
            doc,
            processed_objects=processed_objects,
            processed_size=str(processed_bytes),
            progress=progress,
            message=message or doc.message,
        )
        last_saved_objects = processed_objects
        last_saved_bytes = processed_bytes
        last_saved_at = now_mono

    return update


def _prepare_doc(doc, total_objects: int, total_bytes: int, message: str):
    _save_operation(
        doc,
        status="Running",
        progress=0,
        total_objects=total_objects,
        processed_objects=0,
        failed_objects=0,
        total_size=str(total_bytes),
        processed_size="0",
        started_on=doc.started_on or now_datetime(),
        message=message,
        error_message=None,
    )


def _complete(doc, message: str, **extra):
    values = {
        "status": "Completed",
        "progress": 100,
        "processed_objects": doc.total_objects,
        "processed_size": doc.total_size,
        "completed_on": now_datetime(),
        "message": message,
        "error_message": None,
        **extra,
    }
    _save_operation(doc, **values)


def _run_transfer(doc, payload, mode: str):
    bucket = get_bucket(doc.connection, check_permission=False)
    items = payload.get("items") or []
    destination_prefix = payload.get("destination_prefix") or ""
    conflict_strategy = payload.get("conflict_strategy") or "fail"
    new_name = payload.get("new_name")

    plan = prepare_transfer_plan(
        bucket=bucket,
        items=items,
        destination_parent=destination_prefix,
        conflict_strategy=conflict_strategy,
        rename_to=new_name,
    )
    total_bytes = sum(int(row.get("Size") or 0) for row in plan)
    _prepare_doc(
        doc,
        total_objects=len(plan),
        total_bytes=total_bytes,
        message=_("Copying objects") if mode == "copy" else _("Moving objects"),
    )

    result = execute_transfer_plan(
        bucket=bucket,
        plan=plan,
        mode=mode,
        update_linked_records=bool(payload.get("update_linked_records", True)),
        user=payload.get("requested_by") or doc.started_by,
        progress=_progress_callback(doc),
    )

    destination_keys = [row["relative_destination"] for row in plan]
    if destination_keys:
        if len(destination_keys) == 1:
            doc.destination_key = destination_keys[0]
        else:
            doc.destination_key = destination_prefix

    _complete(
        doc,
        _("{0} object(s) processed successfully").format(result["objects"]),
    )


def _run_delete(doc, payload):
    bucket = get_bucket(doc.connection, check_permission=False)
    items = payload.get("items") or []
    plan = prepare_delete_plan(bucket, items)
    total_bytes = sum(int(row.get("Size") or 0) for row in plan)
    _prepare_doc(
        doc,
        total_objects=len(plan),
        total_bytes=total_bytes,
        message=_("Deleting objects"),
    )
    result = execute_delete_plan(
        bucket=bucket,
        plan=plan,
        allow_linked_delete=bool(payload.get("allow_linked_delete")),
        user=payload.get("requested_by") or doc.started_by,
        progress=_progress_callback(doc),
    )
    _complete(
        doc,
        _("Deleted {0} object(s)").format(result["objects"]),
    )


def _archive_filename(doc, payload) -> str:
    items = payload.get("items") or []
    if len(items) == 1 and items[0].get("type") == "folder":
        base = str(items[0].get("name") or "folder").strip() or "folder"
    else:
        base = "selected-s3-files"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in base)
    safe = safe.strip("._") or "s3-download"
    return f"{safe}.zip"


def _run_zip(doc, payload):
    bucket = get_bucket(doc.connection, check_permission=False)
    items = payload.get("items") or []
    plan = prepare_zip_plan(bucket, items)
    total_bytes = sum(int(row.get("Size") or 0) for row in plan)
    _prepare_doc(
        doc,
        total_objects=len(plan),
        total_bytes=total_bytes,
        message=_("Creating ZIP archive"),
    )

    user_hash = hashlib.sha256(str(doc.started_by).encode("utf-8")).hexdigest()[:12]
    filename = _archive_filename(doc, payload)
    relative_archive_key = normalize_relative_path(
        f".s3-vault-temp/downloads/{user_hash}/{doc.name}/{filename}"
    )
    storage_archive_key = full_key(bucket, relative_archive_key)

    result = create_zip_archive(
        bucket=bucket,
        plan=plan,
        archive_storage_key=storage_archive_key,
        progress=_progress_callback(doc),
    )
    expires_on = add_to_date(
        now_datetime(),
        hours=TEMP_ARCHIVE_RETENTION_HOURS,
        as_datetime=True,
    )

    manager_log(
        action="Generate URL",
        bucket=bucket,
        object_key=storage_archive_key,
        user=payload.get("requested_by") or doc.started_by,
        message=f"ZIP operation={doc.name}; source_files={result['source_files']}",
    )

    _complete(
        doc,
        _("ZIP archive created: {0}").format(filename),
        result_key=relative_archive_key,
        result_file_name=filename,
        result_expires_on=expires_on,
        result_deleted=0,
    )


def run_operation(operation_name: str):
    if not operation_name or not frappe.db.exists("S3 Vault Operation", operation_name):
        return

    doc = frappe.get_doc("S3 Vault Operation", operation_name)
    if doc.status in {"Completed", "Partially Completed", "Cancelled"}:
        return

    payload = parse_json(doc.operation_payload, default={}) or {}
    try:
        if doc.operation_type in {"Bulk Copy", "Copy Folder"}:
            _run_transfer(doc, payload, mode="copy")
        elif doc.operation_type in {"Bulk Move", "Move Folder", "Rename Folder"}:
            _run_transfer(doc, payload, mode="move")
        elif doc.operation_type in {"Bulk Delete", "Delete Folder"}:
            _run_delete(doc, payload)
        elif doc.operation_type in {"Download Folder ZIP", "Bulk Download ZIP"}:
            _run_zip(doc, payload)
        else:
            frappe.throw(_("Unsupported S3 Vault operation: {0}").format(doc.operation_type))
    except Exception as exc:
        traceback_text = frappe.get_traceback()
        try:
            bucket = get_bucket(doc.connection, check_permission=False)
            manager_log(
                action="Error",
                bucket=bucket,
                object_key=doc.source_key,
                status="Failed",
                user=doc.started_by,
                message=f"operation={doc.name}; {exc}",
                traceback_text=traceback_text,
            )
        except Exception:
            pass

        _save_operation(
            doc,
            status="Failed",
            completed_on=now_datetime(),
            message=_("Operation failed"),
            error_message=str(exc),
        )
        frappe.log_error(
            traceback_text,
            f"S3 Vault Operation Failed: {doc.name}",
        )
        raise


def cleanup_expired_archives():
    rows = frappe.get_all(
        "S3 Vault Operation",
        filters={
            "status": "Completed",
            "result_deleted": 0,
            "result_key": ["is", "set"],
            "result_expires_on": ["<", now_datetime()],
        },
        fields=["name", "connection", "result_key", "started_by"],
        limit=200,
    )

    for row in rows:
        try:
            bucket = get_bucket(row.connection, check_permission=False)
            storage_key = full_key(bucket, row.result_key)
            errors = delete_storage_keys(bucket, [storage_key])
            if errors:
                raise RuntimeError(str(errors))
            frappe.db.set_value(
                "S3 Vault Operation",
                row.name,
                {
                    "result_deleted": 1,
                    "message": _("Generated archive expired and was deleted"),
                },
                update_modified=True,
            )
            manager_log(
                action="Delete",
                bucket=bucket,
                object_key=storage_key,
                user=row.started_by,
                message=f"Expired temporary archive for operation={row.name}",
            )
            frappe.db.commit()
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"S3 Vault Archive Cleanup Failed: {row.name}",
            )
