import os
import re
from urllib.parse import quote, unquote

import frappe


def qname(name):
    return "`" + name.replace("`", "``") + "`"


def secure_url(file_id):
    return f"/api/method/frappe_s3_vault.api.download?file={file_id}"


def safe_log(file_id, action, status="Success", message=None):
    try:
        from frappe_s3_vault.utils import insert_log
        insert_log(
            action=action,
            status=status,
            file=file_id,
            error_message=message,
        )
    except Exception:
        try:
            doc = frappe.new_doc("S3 Vault Log")
            if "action" in doc.meta.get_fieldnames():
                doc.action = action
            if "status" in doc.meta.get_fieldnames():
                doc.status = status
            if "file" in doc.meta.get_fieldnames():
                doc.file = file_id
            if "error_message" in doc.meta.get_fieldnames():
                doc.error_message = message
            doc.flags.ignore_permissions = True
            doc.insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "S3 Vault Delete Log Failed")


def get_vault_rows(file_id):
    return frappe.get_all(
        "S3 Vault File",
        filters={"file": file_id},
        fields=["name", "file", "status", "object_key", "bucket", "local_file_url"],
        order_by="creation desc",
    )


def get_text_columns(table):
    db = frappe.local.conf.db_name

    rows = frappe.db.sql(
        """
        select column_name, data_type
        from information_schema.columns
        where table_schema=%s
          and table_name=%s
          and data_type in ('varchar','text','mediumtext','longtext','json')
        """,
        (db, table),
        as_dict=True,
    )

    return [r.column_name for r in rows if r.column_name != "name"]


def get_raven_tables():
    db = frappe.local.conf.db_name

    rows = frappe.db.sql(
        """
        select table_name
        from information_schema.tables
        where table_schema=%s
          and table_name like 'tabRaven%%'
        """,
        db,
        as_dict=True,
    )

    return [r.table_name for r in rows]


def build_deleted_variants(file_id, file_doc=None, vault_rows=None):
    values = set()

    values.add(file_id)
    values.add(secure_url(file_id))

    if file_doc:
        for v in [
            file_doc.get("file_url"),
            file_doc.get("file_name"),
            file_doc.get("old_file_url"),
        ]:
            if v:
                values.add(v)

    for row in vault_rows or []:
        for v in [row.get("object_key"), row.get("local_file_url")]:
            if v:
                values.add(v)
                values.add(os.path.basename(v))

        if row.get("object_key"):
            base = os.path.basename(row.object_key)
            values.add(base)
            values.add("/private/files/" + base)
            values.add("/files/" + base)

    more = set()
    for v in values:
        if not v:
            continue
        more.add(v)
        more.add(unquote(v))
        more.add(quote(unquote(v), safe="/:?=&"))
        more.add(v.replace("/", "\\/"))

    return sorted([x for x in more if x], key=len, reverse=True)


def replace_deleted_refs(text, variants):
    if not isinstance(text, str):
        return text

    result = text

    for v in variants:
        if not v:
            continue
        result = result.replace(v, "[deleted file]")

    # Remove complete API download links for this app
    result = re.sub(
        r"/api/method/frappe_s3_vault\.api\.download\?file=[A-Za-z0-9]+",
        "[deleted file]",
        result,
    )

    # Remove absolute API download links
    result = re.sub(
        r"https?://[^\"'\s<>)]+/api/method/frappe_s3_vault\.api\.download\?file=[A-Za-z0-9]+",
        "[deleted file]",
        result,
    )

    return result



def _doctype_from_raven_table(table):
    table = str(table or "")
    if table.startswith("tab"):
        return table[3:]
    return table


def clean_raven_message_for_deleted_file(file_id):
    file_doc = None

    if frappe.db.exists("File", file_id):
        file_doc = frappe.db.get_value(
            "File",
            file_id,
            ["name", "file_name", "file_url", "attached_to_doctype", "attached_to_name"],
            as_dict=True,
        )

    vault_rows = get_vault_rows(file_id)
    variants = build_deleted_variants(file_id, file_doc, vault_rows)

    changed = 0

    for table in get_raven_tables():
        columns = get_text_columns(table)
        if not columns:
            continue

        doctype = _doctype_from_raven_table(table)

        try:
            rows = frappe.get_all(
                doctype,
                fields=["name"] + columns,
                limit_page_length=0,
                ignore_permissions=True,
            )
        except Exception:
            continue

        for row in rows:
            updates = {}

            for col in columns:
                old = row.get(col)
                new = replace_deleted_refs(old, variants)

                if new != old:
                    updates[col] = new

            if updates:
                frappe.db.set_value(
                    doctype,
                    row.name,
                    updates,
                    update_modified=False,
                )
                changed += 1

    return f"Cleaned Raven deleted-file references. Changed rows: {changed}"

def mark_vault_deleted_and_release_links(file_id):
    rows = get_vault_rows(file_id)

    for row in rows:
        # Mark deleted
        try:
            frappe.db.set_value("S3 Vault File", row.name, "status", "Deleted", update_modified=False)
        except Exception:
            pass

        for field, value in [
            ("deleted_from_storage", 1),
            ("deleted_on", frappe.utils.now()),
            ("deleted_by", frappe.session.user),
        ]:
            try:
                if frappe.get_meta("S3 Vault File").has_field(field):
                    frappe.db.set_value("S3 Vault File", row.name, field, value, update_modified=False)
            except Exception:
                pass

        # Important: release Link to File so File deletion is not blocked
        try:
            frappe.db.sql(
                "update `tabS3 Vault File` set file=NULL where name=%s",
                row.name,
            )
        except Exception:
            try:
                frappe.db.sql(
                    "update `tabS3 Vault File` set file='' where name=%s",
                    row.name,
                )
            except Exception:
                pass

    frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.


def release_log_links(file_id):
    # S3 Vault Log should not block File deletion.
    try:
        if frappe.db.has_column("S3 Vault Log", "file"):
            frappe.db.sql(
                "update `tabS3 Vault Log` set file=NULL where file=%s",
                file_id,
            )
            frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.
    except Exception:
        try:
            frappe.db.sql(
                "update `tabS3 Vault Log` set file='' where file=%s",
                file_id,
            )
            frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.
        except Exception:
            pass


def handle_file_delete(file_doc):
    file_id = file_doc.name

    try:
        # 1. Clean Raven message text/JSON so deleted attachment is not clickable
        clean_raven_message_for_deleted_file(file_id)

        # 2. Mark S3 Vault File deleted and release its Link to File
        mark_vault_deleted_and_release_links(file_id)

        # 3. Record delete log
        safe_log(file_id, "Delete", "Success", "File deleted from storage and local references cleaned")

        # 4. Release log links after recording, so File delete is not blocked
        release_log_links(file_id)

        frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.
    except Exception:
        safe_log(file_id, "Delete", "Failed", frappe.get_traceback())
        frappe.log_error(frappe.get_traceback(), "S3 Vault Delete Handler Failed")


def cleanup_deleted_file_now(file_id):
    fake = frappe._dict({"name": file_id})
    handle_file_delete(fake)
    return f"Delete cleanup completed for {file_id}"
