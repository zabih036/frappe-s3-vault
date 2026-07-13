from __future__ import annotations

import hashlib
import mimetypes
import uuid
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from frappe_s3_vault.file_manager_common import (
    basename,
    content_type_for_name,
    full_key,
    get_bucket,
    get_s3_client,
    iso,
    normalize_relative_path,
    relative_key,
    root_prefix,
)
from frappe_s3_vault.file_manager_permissions import (
    accessible_roots,
    capabilities_for,
    is_admin,
    require_access,
)

INDEX_BATCH_SIZE = 500
SEARCH_LIMIT_MAX = 200


def index_id(connection: str, object_key: str) -> str:
    return hashlib.sha256(f"{connection}\0{object_key}".encode("utf-8")).hexdigest()


def _parent_prefix(relative: str) -> str:
    relative = normalize_relative_path(relative)
    if "/" not in relative:
        return ""
    return relative.rsplit("/", 1)[0] + "/"


def _folder_rows(bucket, relative: str, run_id: str) -> list[dict]:
    parts = normalize_relative_path(relative).split("/")[:-1]
    rows = []
    current = ""
    for part in parts:
        current += f"{part}/"
        storage_key = full_key(bucket, current, folder=True)
        rows.append(
            {
                "index_id": index_id(bucket.name, storage_key),
                "connection": bucket.name,
                "bucket_name": bucket.bucket_name,
                "object_key": storage_key,
                "relative_key": current,
                "file_name": part,
                "parent_prefix": _parent_prefix(current.rstrip("/")),
                "is_folder": 1,
                "size": 0,
                "content_type": "application/x-directory",
                "etag": None,
                "last_modified": None,
                "storage_class": None,
                "index_run_id": run_id,
                "indexed_on": now_datetime(),
            }
        )
    return rows


def object_row(bucket, storage_key: str, metadata: dict, run_id: str = "live") -> dict:
    relative = relative_key(bucket, storage_key)
    is_folder = int(storage_key.endswith("/"))
    filename = relative.rstrip("/").rsplit("/", 1)[-1] if relative else ""
    return {
        "index_id": index_id(bucket.name, storage_key),
        "connection": bucket.name,
        "bucket_name": bucket.bucket_name,
        "object_key": storage_key,
        "relative_key": relative,
        "file_name": filename,
        "parent_prefix": _parent_prefix(relative.rstrip("/")),
        "is_folder": is_folder,
        "size": int(metadata.get("Size", metadata.get("ContentLength", 0)) or 0),
        "content_type": metadata.get("ContentType")
        or ("application/x-directory" if is_folder else content_type_for_name(filename)),
        "etag": str(metadata.get("ETag") or "").strip('"') or None,
        "last_modified": metadata.get("LastModified"),
        "storage_class": metadata.get("StorageClass"),
        "index_run_id": run_id,
        "indexed_on": now_datetime(),
    }


def upsert_rows(rows: list[dict]):
    if not rows or not frappe.db.exists("DocType", "S3 Vault Object Index"):
        return
    # Deduplicate the batch by deterministic name. Folder rows can occur repeatedly.
    deduped = {row["index_id"]: row for row in rows}
    rows = list(deduped.values())
    columns = [
        "name",
        "index_id",
        "creation",
        "modified",
        "modified_by",
        "owner",
        "docstatus",
        "idx",
        "connection",
        "bucket_name",
        "object_key",
        "relative_key",
        "file_name",
        "parent_prefix",
        "is_folder",
        "size",
        "content_type",
        "etag",
        "last_modified",
        "storage_class",
        "index_run_id",
        "indexed_on",
    ]
    now = now_datetime()
    for start in range(0, len(rows), INDEX_BATCH_SIZE):
        batch = rows[start : start + INDEX_BATCH_SIZE]
        values = []
        placeholders = []
        for row in batch:
            placeholders.append("(" + ",".join(["%s"] * len(columns)) + ")")
            values.extend(
                [
                    row["index_id"],
                    row["index_id"],
                    now,
                    now,
                    "Administrator",
                    "Administrator",
                    0,
                    0,
                    row["connection"],
                    row["bucket_name"],
                    row["object_key"],
                    row["relative_key"],
                    row["file_name"],
                    row["parent_prefix"],
                    int(row.get("is_folder") or 0),
                    str(int(row.get("size") or 0)),
                    row.get("content_type"),
                    row.get("etag"),
                    row.get("last_modified"),
                    row.get("storage_class"),
                    row.get("index_run_id") or "live",
                    row.get("indexed_on") or now,
                ]
            )
        quoted = ",".join(f"`{column}`" for column in columns)
        query = f"""
            INSERT INTO `tabS3 Vault Object Index` ({quoted})
            VALUES {','.join(placeholders)}
            ON DUPLICATE KEY UPDATE
                `modified`=VALUES(`modified`),
                `modified_by`=VALUES(`modified_by`),
                `bucket_name`=VALUES(`bucket_name`),
                `object_key`=VALUES(`object_key`),
                `relative_key`=VALUES(`relative_key`),
                `file_name`=VALUES(`file_name`),
                `parent_prefix`=VALUES(`parent_prefix`),
                `is_folder`=VALUES(`is_folder`),
                `size`=VALUES(`size`),
                `content_type`=VALUES(`content_type`),
                `etag`=VALUES(`etag`),
                `last_modified`=VALUES(`last_modified`),
                `storage_class`=VALUES(`storage_class`),
                `index_run_id`=VALUES(`index_run_id`),
                `indexed_on`=VALUES(`indexed_on`)
        """
        frappe.db.sql(query, values)


def sync_single_object(bucket, storage_key: str):
    if not frappe.db.exists("DocType", "S3 Vault Object Index"):
        return
    if not frappe.db.exists("S3 Vault Index Configuration", bucket.name):
        return
    config = frappe.get_doc("S3 Vault Index Configuration", bucket.name)
    if not cint(config.enabled):
        return
    client = get_s3_client(bucket)
    try:
        head = client.head_object(Bucket=bucket.bucket_name, Key=storage_key)
    except Exception:
        remove_index_keys(bucket.name, [storage_key])
        return
    row = object_row(bucket, storage_key, head, run_id="live")
    rows = [row]
    if cint(config.include_folder_rows) and not row["is_folder"]:
        rows.extend(_folder_rows(bucket, row["relative_key"], "live"))
    upsert_rows(rows)


def remove_index_keys(connection: str, storage_keys: list[str]):
    names = [index_id(connection, key) for key in storage_keys if key]
    if not names or not frappe.db.exists("DocType", "S3 Vault Object Index"):
        return
    for start in range(0, len(names), 500):
        frappe.db.delete("S3 Vault Object Index", {"name": ["in", names[start : start + 500]]})


def apply_transfer_index(bucket, key_map: dict[str, str], mode: str):
    for source, destination in key_map.items():
        sync_single_object(bucket, destination)
    if mode == "move":
        remove_index_keys(bucket.name, list(key_map))


def _config(connection: str):
    if not frappe.db.exists("S3 Vault Index Configuration", connection):
        return None
    return frappe.get_doc("S3 Vault Index Configuration", connection)


def next_sync(frequency: str, from_time=None):
    value = from_time or now_datetime()
    if frequency == "Every 6 Hours":
        return add_to_date(value, hours=6, as_datetime=True)
    if frequency == "Daily":
        return add_to_date(value, days=1, as_datetime=True)
    if frequency == "Weekly":
        return add_to_date(value, days=7, as_datetime=True)
    return None


def _set_config(connection: str, **values):
    if not frappe.db.exists("S3 Vault Index Configuration", connection):
        return
    frappe.db.set_value(
        "S3 Vault Index Configuration",
        connection,
        values,
        update_modified=True,
    )


def run_index_rebuild(doc, payload: dict, progress=None):
    bucket = get_bucket(doc.connection, check_permission=False)
    config = _config(bucket.name)
    include_folders = bool(cint(getattr(config, "include_folder_rows", 0))) if config else False
    run_id = uuid.uuid4().hex
    client = get_s3_client(bucket)
    paginator = client.get_paginator("list_objects_v2")
    object_count = 0
    folder_count = 0
    total_size = 0
    seen_folders: set[str] = set()
    _set_config(bucket.name, status="Running", error_message=None, last_operation=doc.name)

    try:
        for page in paginator.paginate(Bucket=bucket.bucket_name, Prefix=root_prefix(bucket)):
            rows: list[dict] = []
            page_bytes = 0
            for item in page.get("Contents", []):
                storage_key = item.get("Key") or ""
                if not storage_key:
                    continue
                relative_storage_key = relative_key(bucket, storage_key)
                if relative_storage_key.startswith(".s3-vault-temp/"):
                    continue
                row = object_row(bucket, storage_key, item, run_id)
                rows.append(row)
                if row["is_folder"]:
                    seen_folders.add(row["object_key"])
                    folder_count += 1
                else:
                    object_count += 1
                    total_size += int(row["size"])
                    page_bytes += int(row["size"])
                    if include_folders:
                        for folder_row in _folder_rows(bucket, row["relative_key"], run_id):
                            if folder_row["object_key"] in seen_folders:
                                continue
                            seen_folders.add(folder_row["object_key"])
                            rows.append(folder_row)
                            folder_count += 1
            upsert_rows(rows)
            if progress:
                progress(len(rows), page_bytes, _("Indexed {0} objects").format(object_count))
            frappe.db.commit()

        frappe.db.sql(
            """
            DELETE FROM `tabS3 Vault Object Index`
            WHERE `connection`=%s AND `index_run_id`!=%s
            """,
            (bucket.name, run_id),
        )
        completed = now_datetime()
        _set_config(
            bucket.name,
            status="Completed",
            last_sync_on=completed,
            next_sync_on=next_sync(config.sync_frequency, completed) if config else None,
            object_count=object_count,
            folder_count=folder_count,
            total_size=str(total_size),
            error_message=None,
            last_operation=doc.name,
        )
        frappe.db.commit()
        return {
            "objects": object_count,
            "folders": folder_count,
            "bytes": total_size,
        }
    except Exception as exc:
        status = "Cancelled" if exc.__class__.__name__ == "OperationCancelled" else "Failed"
        _set_config(bucket.name, status=status, error_message=str(exc), last_operation=doc.name)
        frappe.db.commit()
        raise


def _root_sql(roots: list[str], values: list) -> str:
    if roots == [""]:
        return "1=1"
    clauses = []
    for root in roots:
        # LEFT avoids treating %, _ or backslashes in an S3 prefix as SQL wildcards.
        clauses.append("LEFT(`relative_key`, %s)=%s")
        values.extend([len(root), root])
    return "(" + " OR ".join(clauses) + ")"


@frappe.whitelist()
def search_index(
    connection: str,
    query: str | None = None,
    prefix: str | None = None,
    content_type: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_folders: int = 1,
    start: int = 0,
    page_length: int = 50,
):
    roots = accessible_roots(connection)
    if not roots:
        frappe.throw(_("You cannot access this S3 connection."), frappe.PermissionError)
    require_access(connection, prefix or roots[0], "search")
    values: list = [connection]
    conditions = ["`connection`=%s", _root_sql(roots, values)]
    if prefix:
        prefix = normalize_relative_path(prefix, folder=bool(prefix))
        require_access(connection, prefix, "search")
        conditions.append("LEFT(`relative_key`, %s)=%s")
        values.extend([len(prefix), prefix])
    if query:
        conditions.append("(`file_name` LIKE %s OR `relative_key` LIKE %s OR `content_type` LIKE %s)")
        like = f"%{str(query).strip()}%"
        values.extend([like, like, like])
    if content_type:
        conditions.append("`content_type` LIKE %s")
        values.append(f"{content_type}%")
    if not cint(include_folders):
        conditions.append("`is_folder`=0")
    if min_size not in (None, ""):
        conditions.append("CAST(`size` AS UNSIGNED)>=%s")
        values.append(cint(min_size))
    if max_size not in (None, ""):
        conditions.append("CAST(`size` AS UNSIGNED)<=%s")
        values.append(cint(max_size))
    if date_from:
        conditions.append("`last_modified`>=%s")
        values.append(get_datetime(date_from))
    if date_to:
        conditions.append("`last_modified`<=%s")
        values.append(get_datetime(date_to))

    where = " AND ".join(conditions)
    count = frappe.db.sql(
        f"SELECT COUNT(*) FROM `tabS3 Vault Object Index` WHERE {where}", values
    )[0][0]
    page_length = max(1, min(cint(page_length) or 50, SEARCH_LIMIT_MAX))
    start = max(0, cint(start))
    result_values = list(values) + [page_length, start]
    rows = frappe.db.sql(
        f"""
        SELECT `file_name`, `relative_key`, `parent_prefix`, `is_folder`, `size`,
               `content_type`, `etag`, `last_modified`, `storage_class`
        FROM `tabS3 Vault Object Index`
        WHERE {where}
        ORDER BY `last_modified` DESC, `relative_key` ASC
        LIMIT %s OFFSET %s
        """,
        result_values,
        as_dict=True,
    )
    return {
        "rows": [
            {
                "name": row.file_name,
                "key": row.relative_key,
                "type": "folder" if cint(row.is_folder) else "file",
                "size": cint(row.size),
                "content_type": row.content_type,
                "etag": row.etag,
                "last_modified": iso(row.last_modified),
                "storage_class": row.storage_class,
                "parent_prefix": row.parent_prefix,
                "capabilities": capabilities_for(connection, row.relative_key),
            }
            for row in rows
        ],
        "total": cint(count),
        "start": start,
        "page_length": page_length,
        "index_configured": bool(_config(connection)),
    }


@frappe.whitelist()
def get_dashboard(connection: str):
    roots = accessible_roots(connection)
    if not roots:
        frappe.throw(_("You cannot access this S3 connection."), frappe.PermissionError)
    require_access(connection, roots[0], "dashboard")
    values: list = [connection]
    root_condition = _root_sql(roots, values)
    summary = frappe.db.sql(
        f"""
        SELECT COUNT(*) AS total_rows,
               SUM(CASE WHEN `is_folder`=0 THEN 1 ELSE 0 END) AS object_count,
               SUM(CASE WHEN `is_folder`=1 THEN 1 ELSE 0 END) AS folder_count,
               SUM(CASE WHEN `is_folder`=0 THEN CAST(`size` AS UNSIGNED) ELSE 0 END) AS total_size,
               MAX(`indexed_on`) AS indexed_on
        FROM `tabS3 Vault Object Index`
        WHERE `connection`=%s AND {root_condition}
        """,
        values,
        as_dict=True,
    )[0]
    categories = frappe.db.sql(
        f"""
        SELECT
            CASE
                WHEN `content_type` LIKE 'image/%%' THEN 'Images'
                WHEN `content_type` LIKE 'video/%%' THEN 'Videos'
                WHEN `content_type` LIKE 'audio/%%' THEN 'Audio'
                WHEN `content_type`='application/pdf' THEN 'PDF'
                WHEN `content_type` LIKE 'text/%%' THEN 'Text'
                ELSE 'Other'
            END AS category,
            COUNT(*) AS object_count,
            SUM(CAST(`size` AS UNSIGNED)) AS total_size
        FROM `tabS3 Vault Object Index`
        WHERE `connection`=%s AND `is_folder`=0 AND {root_condition}
        GROUP BY category ORDER BY total_size DESC
        """,
        values,
        as_dict=True,
    )
    largest = frappe.db.sql(
        f"""
        SELECT `file_name`, `relative_key`, `size`, `content_type`, `last_modified`
        FROM `tabS3 Vault Object Index`
        WHERE `connection`=%s AND `is_folder`=0 AND {root_condition}
        ORDER BY CAST(`size` AS UNSIGNED) DESC LIMIT 10
        """,
        values,
        as_dict=True,
    )
    config = _config(connection)
    return {
        "summary": dict(summary),
        "categories": [dict(row) for row in categories],
        "largest": [dict(row) for row in largest],
        "configuration": {
            "exists": bool(config),
            "enabled": bool(cint(config.enabled)) if config else False,
            "status": config.status if config else None,
            "last_sync_on": config.last_sync_on if config else None,
            "next_sync_on": config.next_sync_on if config else None,
        },
    }


@frappe.whitelist()
def start_rebuild(connection: str):
    roots = accessible_roots(connection)
    if not roots:
        frappe.throw(_("You cannot access this S3 connection."), frappe.PermissionError)
    require_access(connection, roots[0], "index_rebuild")
    if not frappe.db.exists("S3 Vault Index Configuration", connection):
        if not is_admin():
            frappe.throw(_("An administrator must create an index configuration first."))
        frappe.get_doc(
            {
                "doctype": "S3 Vault Index Configuration",
                "connection": connection,
                "enabled": 1,
                "sync_frequency": "Manual",
                "include_folder_rows": 1,
                "status": "Never Synced",
            }
        ).insert(ignore_permissions=True)
    from frappe_s3_vault.file_manager import _create_operation

    bucket = get_bucket(connection, check_permission=False)
    operation = _create_operation(
        operation_type="Rebuild Object Index",
        bucket=bucket,
        source_key=None,
        destination_key=None,
        payload={"requested_by": frappe.session.user},
    )
    _set_config(connection, status="Queued", last_operation=operation["name"], error_message=None)
    return operation


def enqueue_due_index_syncs():
    if not frappe.db.exists("DocType", "S3 Vault Index Configuration"):
        return
    rows = frappe.get_all(
        "S3 Vault Index Configuration",
        filters={
            "enabled": 1,
            "sync_frequency": ["!=", "Manual"],
            "next_sync_on": ["<=", now_datetime()],
            "status": ["not in", ["Queued", "Running"]],
        },
        fields=["name", "connection"],
        limit=20,
    )
    for row in rows:
        try:
            bucket = get_bucket(row.connection, check_permission=False)
            doc = frappe.get_doc(
                {
                    "doctype": "S3 Vault Operation",
                    "operation_type": "Rebuild Object Index",
                    "connection": bucket.name,
                    "bucket_name": bucket.bucket_name,
                    "status": "Queued",
                    "progress": 0,
                    "started_by": "Administrator",
                    "message": _("Waiting for a background worker"),
                    "operation_payload": frappe.as_json({"requested_by": "Administrator"}),
                }
            )
            doc.insert(ignore_permissions=True)
            job_id = f"s3vault-operation-{doc.name}"[:140]
            doc.db_set("background_job_id", job_id, update_modified=False)
            frappe.enqueue(
                "frappe_s3_vault.file_manager_jobs.run_operation",
                queue="long",
                timeout=21_600,
                job_id=job_id,
                enqueue_after_commit=True,
                operation_name=doc.name,
            )
            _set_config(row.connection, status="Queued", last_operation=doc.name)
            frappe.db.commit()
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"S3 Vault Scheduled Index Sync Failed: {row.connection}",
            )
