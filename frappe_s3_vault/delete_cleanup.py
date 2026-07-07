import os
import frappe


DOWNLOAD_PREFIX = "/api/method/frappe_s3_vault.api.download"


def qname(name):
    return "`" + name.replace("`", "``") + "`"


def table_exists(table):
    return bool(
        frappe.db.sql(
            """
            select count(*) as c
            from information_schema.tables
            where table_schema = database()
              and table_name = %s
            """,
            table,
            as_dict=True,
        )[0].c
    )


def table_columns(table):
    return frappe.db.sql(
        """
        select column_name, data_type
        from information_schema.columns
        where table_schema = database()
          and table_name = %s
        """,
        table,
        as_dict=True,
    )


def get_file_doc(file_id):
    if file_id and frappe.db.exists("File", file_id):
        return frappe.get_doc("File", file_id)
    return None


def get_storage_doc(file_id):
    name = frappe.db.get_value(
        "S3 Vault File",
        {"file": file_id},
        "name",
        order_by="creation desc",
    )

    if name:
        return frappe.get_doc("S3 Vault File", name)

    return None


def get_variants(file_id, file_doc=None, storage_doc=None):
    values = set()

    values.add(file_id)
    values.add(f"{DOWNLOAD_PREFIX}?file={file_id}")

    if file_doc:
        values.add(file_doc.file_url)
        values.add(file_doc.file_name)
        if file_doc.file_url:
            values.add(os.path.basename(file_doc.file_url))

    if storage_doc:
        values.add(storage_doc.get("object_key"))
        values.add(storage_doc.get("local_file_url"))
        values.add(storage_doc.get("original_file_name"))
        values.add(storage_doc.get("stored_file_name"))

        if storage_doc.get("object_key"):
            values.add(os.path.basename(storage_doc.get("object_key")))

    return [v for v in values if v]


def clear_raven_attachment(file_id, file_doc=None, storage_doc=None, dry_run=1):
    """
    Clear Raven message attachment fields so the file card disappears.

    This targets:
    - tabRaven Message row by attached_to_name
    - fields such as file, file_url, attachment, image, thumbnail
    - text/json columns containing the file URL/key
    """

    dry_run = int(dry_run or 0)

    message_name = None

    if file_doc:
        message_name = file_doc.get("attached_to_name")

    if not message_name and storage_doc:
        message_name = storage_doc.get("attached_to_name")

    if not message_name:
        return {
            "status": "skipped",
            "reason": "No Raven Message name found",
        }

    table = "tabRaven Message"

    if not table_exists(table):
        return {
            "status": "skipped",
            "reason": "tabRaven Message table not found",
            "message_name": message_name,
        }

    cols = table_columns(table)
    col_names = [c.column_name for c in cols]

    if "name" not in col_names:
        return {
            "status": "skipped",
            "reason": "Raven Message table has no name column",
        }

    if not frappe.db.exists("Raven Message", message_name):
        return {
            "status": "skipped",
            "reason": "Raven Message row does not exist",
            "message_name": message_name,
        }

    variants = get_variants(file_id, file_doc, storage_doc)

    file_like_fields = [
        c.column_name
        for c in cols
        if any(
            key in c.column_name.lower()
            for key in ["file", "attachment", "image", "thumbnail"]
        )
    ]

    text_fields = [
        c.column_name
        for c in cols
        if c.data_type in ["varchar", "text", "mediumtext", "longtext", "json"]
    ]

    actions = {
        "message_name": message_name,
        "clear_fields": file_like_fields,
        "replace_text_fields": text_fields,
        "dry_run": dry_run,
    }

    if dry_run:
        return actions

    # Clear direct attachment/file fields.
    for field in file_like_fields:
        if field == "name":
            continue

        try:
            frappe.db.set_value("Raven Message", message_name, field, None, update_modified=False)
        except Exception:
            try:
                frappe.db.set_value("Raven Message", message_name, field, "", update_modified=False)
            except Exception:
                pass

    # Replace old URL/key text references.
    for field in text_fields:
        if field == "name":
            continue

        try:
            current = frappe.db.get_value("Raven Message", message_name, field)
        except Exception:
            continue

        if not current:
            continue

        new_value = str(current)

        for variant in variants:
            new_value = new_value.replace(str(variant), "[deleted file]")

        if new_value != str(current):
            try:
                frappe.db.set_value("Raven Message", message_name, field, new_value, update_modified=False)
            except Exception:
                pass

    return actions


def release_log_file_links(file_id):
    try:
        frappe.db.sql(
            "update `tabS3 Vault Log` set file=NULL where file=%s",
            file_id,
        )
    except Exception:
        try:
            frappe.db.sql(
                "update `tabS3 Vault Log` set file='' where file=%s",
                file_id,
            )
        except Exception:
            pass


def mark_storage_deleted(file_id, storage_doc=None):
    if not storage_doc:
        storage_doc = get_storage_doc(file_id)

    if not storage_doc:
        return None

    storage_doc.status = "Deleted"
    storage_doc.deleted_from_storage = 1
    storage_doc.deleted_on = frappe.utils.now()
    storage_doc.deleted_by = frappe.session.user
    storage_doc.flags.ignore_permissions = True
    storage_doc.save(ignore_permissions=True)

    return storage_doc


def delete_file_doc(file_id):
    if not frappe.db.exists("File", file_id):
        return "File row already deleted"

    frappe.flags.s3_vault_skip_file_delete_hook = True

    try:
        frappe.delete_doc(
            "File",
            file_id,
            force=True,
            ignore_permissions=True,
        )
        return "File row deleted"
    finally:
        frappe.flags.s3_vault_skip_file_delete_hook = False


def finalize_deleted_file(file_id, dry_run=1, delete_file_record=1):
    """
    Final cleanup for cases where the storage object is gone but:
    - Frappe File row still exists
    - Raven file card still remains
    - Delete log was not created

    Use dry_run=1 first.
    """

    dry_run = int(dry_run or 0)
    delete_file_record = int(delete_file_record or 0)

    file_doc = get_file_doc(file_id)
    storage_doc = get_storage_doc(file_id)

    if not file_doc and not storage_doc:
        return {
            "file": file_id,
            "status": "skipped",
            "reason": "No File row and no S3 Vault File row found",
        }

    result = {
        "file": file_id,
        "dry_run": dry_run,
        "file_exists": bool(file_doc),
        "storage_file": storage_doc.name if storage_doc else None,
        "current_storage_status": storage_doc.status if storage_doc else None,
        "attached_to_doctype": (file_doc.get("attached_to_doctype") if file_doc else storage_doc.get("attached_to_doctype")),
        "attached_to_name": (file_doc.get("attached_to_name") if file_doc else storage_doc.get("attached_to_name")),
    }

    result["raven_cleanup"] = clear_raven_attachment(
        file_id=file_id,
        file_doc=file_doc,
        storage_doc=storage_doc,
        dry_run=dry_run,
    )

    if dry_run:
        result["actions"] = [
            "write Delete log",
            "mark S3 Vault File Deleted",
            "release S3 Vault Log file links",
            "release S3 Vault File file link",
            "delete File row" if delete_file_record else "keep File row",
        ]
        return result

    # Write Delete log before releasing links.
    try:
        from frappe_s3_vault.logs import write_log

        write_log(
            action="Delete",
            status="Success",
            file_id=file_id,
            storage_file=storage_doc.name if storage_doc else None,
            bucket_name=storage_doc.get("bucket_name") if storage_doc else None,
            object_key=storage_doc.get("object_key") if storage_doc else None,
            error_message="Storage object already missing; cleaned Raven attachment and File record.",
            commit=False,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Manual Delete Log Failed")

    # Mark storage audit row deleted.
    if storage_doc:
        storage_doc = mark_storage_deleted(file_id, storage_doc)

    # Release links before deleting File row.
    release_log_file_links(file_id)

    try:
        from frappe_s3_vault.records import release_file_link_from_vault
        release_file_link_from_vault(file_id)
    except Exception:
        pass

    if delete_file_record:
        result["file_delete_result"] = delete_file_doc(file_id)

    frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.

    result["status"] = "completed"

    return result


def finalize_all_missing_files(dry_run=1, limit=20):
    rows = frappe.get_all(
        "S3 Vault File",
        filters={"status": "Missing"},
        fields=["file", "name", "object_key"],
        order_by="modified desc",
        limit=int(limit),
    )

    out = []

    for row in rows:
        if row.file:
            out.append(finalize_deleted_file(row.file, dry_run=dry_run, delete_file_record=1))

    return {
        "dry_run": int(dry_run or 0),
        "count": len(out),
        "results": out,
    }


# final override: remove Raven visible file card structure
def raven_column_exists(fieldname):
    try:
        return frappe.get_meta("Raven Message").has_field(fieldname)
    except Exception:
        return False


def sql_set_raven_field(message_name, fieldname, value):
    if not raven_column_exists(fieldname):
        return False

    frappe.db.set_value("Raven Message", message_name, fieldname, value, update_modified=False)
    return True


def get_raven_message_type_text_value():
    try:
        df = frappe.get_meta("Raven Message").get_field("message_type")
        options = [x.strip() for x in str(df.options or "").split("\n") if x.strip()]

        for candidate in ["Text", "text", "Message", "message"]:
            if candidate in options:
                return candidate
    except Exception:
        pass

    return "Text"


def remove_raven_file_card(message_name, dry_run=1):
    """
    Force Raven message UI to stop rendering a file/image attachment card.

    This is needed because clearing File row is not enough. Raven UI may still render
    the file structure from message_type/content/json.
    """

    dry_run = int(dry_run or 0)

    if not message_name:
        return {
            "status": "skipped",
            "reason": "message_name missing",
        }

    if not frappe.db.exists("Raven Message", message_name):
        return {
            "status": "skipped",
            "reason": "Raven Message not found",
            "message_name": message_name,
        }

    text_value = get_raven_message_type_text_value()

    changes = {
        "message_name": message_name,
        "dry_run": dry_run,
        "set_message_type": text_value,
        "clear_fields": [
            "file",
            "file_thumbnail",
            "blurhash",
            "image_width",
            "image_height",
            "thumbnail_width",
            "thumbnail_height",
            "link_doctype",
            "link_document",
        ],
        "set_text_fields": {
            "text": "This file was deleted.",
            "content": "This file was deleted.",
            "json": "{}",
            "links": "[]",
        },
    }

    if dry_run:
        return changes

    sql_set_raven_field(message_name, "message_type", text_value)

    for field in changes["clear_fields"]:
        sql_set_raven_field(message_name, field, None)

    for field, value in changes["set_text_fields"].items():
        sql_set_raven_field(message_name, field, value)

    frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.

    return {
        "status": "completed",
        "message_name": message_name,
        "message_type": text_value,
    }


def remove_raven_file_card_for_file(file_id, dry_run=1):
    file_doc = get_file_doc(file_id)
    storage_doc = get_storage_doc(file_id)

    message_name = None

    if file_doc:
        message_name = file_doc.get("attached_to_name")

    if not message_name and storage_doc:
        message_name = storage_doc.get("attached_to_name")

    return remove_raven_file_card(message_name, dry_run=dry_run)


def finalize_deleted_file(file_id, dry_run=1, delete_file_record=1):
    """
    Final override.

    Cleanup for cases where storage object is gone but Raven still shows file card.
    """

    dry_run = int(dry_run or 0)
    delete_file_record = int(delete_file_record or 0)

    file_doc = get_file_doc(file_id)
    storage_doc = get_storage_doc(file_id)

    if not file_doc and not storage_doc:
        return {
            "file": file_id,
            "status": "skipped",
            "reason": "No File row and no S3 Vault File row found",
        }

    message_name = None

    if file_doc:
        message_name = file_doc.get("attached_to_name")

    if not message_name and storage_doc:
        message_name = storage_doc.get("attached_to_name")

    result = {
        "file": file_id,
        "dry_run": dry_run,
        "file_exists": bool(file_doc),
        "storage_file": storage_doc.name if storage_doc else None,
        "current_storage_status": storage_doc.status if storage_doc else None,
        "attached_to_doctype": (file_doc.get("attached_to_doctype") if file_doc else storage_doc.get("attached_to_doctype")),
        "attached_to_name": message_name,
        "raven_card_cleanup": remove_raven_file_card(message_name, dry_run=dry_run),
    }

    if dry_run:
        result["actions"] = [
            "write Delete log",
            "force Raven message_type/content/json to normal deleted-text message",
            "mark S3 Vault File Deleted",
            "release S3 Vault Log file links",
            "release S3 Vault File file link",
            "delete File row" if delete_file_record else "keep File row",
        ]
        return result

    try:
        from frappe_s3_vault.logs import write_log

        write_log(
            action="Delete",
            status="Success",
            file_id=file_id,
            storage_file=storage_doc.name if storage_doc else None,
            bucket_name=storage_doc.get("bucket_name") if storage_doc else None,
            object_key=storage_doc.get("object_key") if storage_doc else None,
            error_message="Cleaned Raven file card and deleted File record.",
            commit=False,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Delete Log Failed")

    if storage_doc:
        storage_doc.status = "Deleted"
        storage_doc.deleted_from_storage = 1
        storage_doc.deleted_on = frappe.utils.now()
        storage_doc.deleted_by = frappe.session.user
        storage_doc.flags.ignore_permissions = True
        storage_doc.save(ignore_permissions=True)

    release_log_file_links(file_id)

    try:
        from frappe_s3_vault.records import release_file_link_from_vault
        release_file_link_from_vault(file_id)
    except Exception:
        pass

    if delete_file_record and frappe.db.exists("File", file_id):
        delete_file_doc(file_id)

    frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.

    result["status"] = "completed"
    return result


# stable override: Raven delete must also delete object from Wasabi/S3 when rule allows
def finalize_deleted_file(
    file_id=None,
    dry_run=1,
    delete_file_record=1,
    storage_file=None,
    attached_to_doctype=None,
    attached_to_name=None,
    document_type=None,
    document_name=None,
    commit=True,
    *args,
    **kwargs,
):
    import frappe
    from frappe.utils import cint, now_datetime

    document_type = document_type or attached_to_doctype
    document_name = document_name or attached_to_name

    filters_list = []

    if storage_file:
        filters_list.append({"name": storage_file})

    if file_id:
        filters_list.append({"file": file_id})

    if document_type and document_name:
        filters_list.append({
            "attached_to_doctype": document_type,
            "attached_to_name": document_name,
        })

    # If only file_id is provided but the File row still exists, also search by attachment target.
    if file_id and frappe.db.exists("File", file_id):
        try:
            f = frappe.get_doc("File", file_id)
            if f.attached_to_doctype and f.attached_to_name:
                filters_list.append({
                    "attached_to_doctype": f.attached_to_doctype,
                    "attached_to_name": f.attached_to_name,
                })
        except Exception:
            pass

    if not filters_list:
        return []

    rows_by_name = {}

    for filters in filters_list:
        rows = frappe.get_all(
            "S3 Vault File",
            filters=filters,
            fields=[
                "name",
                "file",
                "status",
                "bucket",
                "bucket_name",
                "object_key",
                "rule_used",
                "attached_to_doctype",
                "attached_to_name",
            ],
            order_by="creation desc",
        )

        for row in rows:
            rows_by_name[row.name] = row

    if not rows_by_name:
        return []

    out = []

    from frappe_s3_vault.logs import write_log

    for row in rows_by_name.values():
        allow_delete = 0

        if row.rule_used and frappe.db.exists("S3 Vault Rule", row.rule_used):
            rule = frappe.get_doc("S3 Vault Rule", row.rule_used)
            allow_delete = cint(getattr(rule, "allow_delete_from_s3", 0))

        item = {
            "storage_file": row.name,
            "file": row.file or file_id,
            "rule_used": row.rule_used,
            "allow_delete_from_s3": allow_delete,
            "bucket_name": row.bucket_name,
            "object_key": row.object_key,
            "status": None,
            "deleted_from_storage": 0,
            "message": None,
        }

        if dry_run:
            item["status"] = "Dry Run"
            out.append(item)
            continue

        try:
            final_status = "Deleted"
            deleted_from_storage = 0

            if allow_delete:
                if not row.bucket or not row.object_key:
                    item["message"] = "Cannot delete from Wasabi because bucket or object_key is missing"
                    final_status = "Deleted"
                    deleted_from_storage = 0
                else:
                    from frappe_s3_vault.utils import s3_client

                    bucket_doc = frappe.get_doc("S3 Vault Bucket", row.bucket)
                    bucket_name = row.bucket_name or bucket_doc.bucket_name

                    client = s3_client(bucket_doc)

                    try:
                        client.delete_object(
                            Bucket=bucket_name,
                            Key=row.object_key,
                        )
                        item["message"] = f"Deleted object {row.object_key} from Wasabi/S3"
                    except Exception:
                        # If object was already manually deleted from Wasabi, still mark storage cleanup complete.
                        item["message"] = "Delete requested; object may already be missing in Wasabi/S3"

                    deleted_from_storage = 1
                    item["bucket_name"] = bucket_name

            else:
                final_status = "Soft Deleted"
                deleted_from_storage = 0
                item["message"] = f"S3 object preserved because rule {row.rule_used} does not allow delete from S3"

            frappe.db.set_value(
                "S3 Vault File",
                row.name,
                {
                    "file": None,
                    "status": final_status,
                    "deleted_from_storage": deleted_from_storage,
                    "deleted_on": now_datetime(),
                    "deleted_by": frappe.session.user,
                },
                update_modified=False,
            )

            # Prevent File deletion/link blocking by audit logs.
            if row.file or file_id:
                frappe.db.sql(
                    """
                    update `tabS3 Vault Log`
                    set file=NULL
                    where file=%s
                    """,
                    row.file or file_id,
                )

            write_log(
                action="Delete",
                status="Success",
                file_id=None,
                storage_file=row.name,
                bucket_name=item["bucket_name"],
                object_key=row.object_key,
                error_message=item["message"],
                commit=False,
            )

            item["status"] = final_status
            item["deleted_from_storage"] = deleted_from_storage

        except Exception:
            item["status"] = "Failed"
            item["message"] = frappe.get_traceback()

            try:
                write_log(
                    action="Delete",
                    status="Failed",
                    file_id=None,
                    storage_file=row.name,
                    bucket_name=row.bucket_name,
                    object_key=row.object_key,
                    error_message="Raven Wasabi delete cleanup failed",
                    traceback_text=frappe.get_traceback(),
                    commit=False,
                )
            except Exception:
                pass

            frappe.log_error(frappe.get_traceback(), "S3 Vault Raven Wasabi Delete Failed")
            raise

        out.append(item)

    # Delete Frappe File record only after S3 Vault links are cleared.
    if delete_file_record and file_id and frappe.db.exists("File", file_id):
        old_flag = getattr(frappe.flags, "s3_vault_skip_file_delete_hook", False)
        frappe.flags.s3_vault_skip_file_delete_hook = True

        try:
            frappe.delete_doc(
                "File",
                file_id,
                ignore_permissions=True,
                force=True,
            )
        finally:
            frappe.flags.s3_vault_skip_file_delete_hook = old_flag

    if commit:
        frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.

    return out
