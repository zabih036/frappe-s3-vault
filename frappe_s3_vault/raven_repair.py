import os
import re
from urllib.parse import quote, unquote

import frappe

DOWNLOAD_PREFIX = "/api/method/frappe_s3_vault.api.download"


def qname(name):
    return "`" + name.replace("`", "``") + "`"


def secure_url(file_name):
    return f"{DOWNLOAD_PREFIX}?file={file_name}"


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


def variants_for_file(file_doc, vault_doc):
    values = set()

    for v in [
        file_doc.file_url,
        file_doc.file_name,
        vault_doc.file_url,
        vault_doc.local_file_url,
        getattr(vault_doc, "original_file_name", None),
        getattr(vault_doc, "file_name", None),
    ]:
        if v:
            values.add(v)
            values.add(quote(v, safe="/:?=&"))
            values.add(unquote(v))

    # Generate /private/files and /files variants from filename
    filenames = set()
    for v in list(values):
        if "/private/files/" in v:
            filenames.add(v.split("/private/files/", 1)[1])
        elif "/files/" in v:
            filenames.add(v.split("/files/", 1)[1])
        elif "." in os.path.basename(v):
            filenames.add(os.path.basename(v))

    for fn in filenames:
        fn = unquote(fn)
        values.add("/private/files/" + fn)
        values.add("/private/files/" + quote(fn))
        values.add("/files/" + fn)
        values.add("/files/" + quote(fn))

    site_url = frappe.utils.get_url().rstrip("/")
    for v in list(values):
        if v.startswith("/"):
            values.add(site_url + v)
            values.add(site_url + quote(v, safe="/:?=&"))

    # JSON escaped slash variant
    for v in list(values):
        values.add(v.replace("/", "\\/"))

    return sorted([v for v in values if v], key=len, reverse=True)


def replace_in_text(value, old_values, new_value):
    if not isinstance(value, str):
        return value

    result = value

    for old in old_values:
        result = result.replace(old, new_value)

    # Catch any absolute URL ending in local file path
    result = re.sub(
        r"https?://[^\"'\s<>)]+/(private/files|files)/[^\"'\s<>)]+",
        lambda m: new_value if any(x in m.group(0) for x in old_values) else m.group(0),
        result,
    )

    return result



def _doctype_from_raven_table(table):
    table = str(table or "")
    if table.startswith("tab"):
        return table[3:]
    return table
\n\ndef repair_raven_for_file(file_name):
    file_doc = frappe.get_doc("File", file_name)

    vault_name = frappe.db.get_value(
        "S3 Vault File",
        {"file": file_name, "status": "Uploaded"},
        "name",
        order_by="creation desc",
    )

    if not vault_name:
        return f"No uploaded S3 Vault File for {file_name}"

    vault_doc = frappe.get_doc("S3 Vault File", vault_name)

    new_url = secure_url(file_name)
    old_values = variants_for_file(file_doc, vault_doc)

    # Always update tabFile
    frappe.db.set_value("File", file_name, "file_url", new_url, update_modified=False)

    changed = 0

    # Repair every Raven table, not only Raven Message meta fields
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
                old_text = row.get(col)
                new_text = replace_in_text(old_text, old_values, new_url)

                if new_text != old_text:
                    updates[col] = new_text

            if updates:
                frappe.db.set_value(
                    doctype,
                    row.name,
                    updates,
                    update_modified=False,
                )
                changed += 1

    frappe.db.commit()
    return f"Repaired Raven links for {file_name}. Changed rows: {changed}. New URL: {new_url}"


def repair_all_raven_links(limit=1000):
    rows = frappe.get_all(
        "S3 Vault File",
        filters={"status": "Uploaded"},
        fields=["file"],
        order_by="creation desc",
        limit=limit,
    )

    fixed = 0

    for r in rows:
        if r.file and frappe.db.exists("File", r.file):
            repair_raven_for_file(r.file)
            fixed += 1

    frappe.db.commit()
    return f"Repaired {fixed} uploaded files in Raven tables"


def find_raven_local_urls(limit=50):
    found = []

    for table in get_raven_tables():
        columns = get_text_columns(table)

        for col in columns:
            try:
                rows = frappe.db.sql(
                    f"""
                    select name, {qname(col)} as value
                    from {qname(table)}
                    where {qname(col)} like %s
                       or {qname(col)} like %s
                    limit {int(limit)}
                    """,
                    ("%/private/files/%", "%/files/%"),
                    as_dict=True,
                )
            except Exception:
                continue

            for r in rows:
                found.append({
                    "table": table,
                    "name": r.name,
                    "column": col,
                    "value": str(r.value)[:300],
                })

    return found[:limit]
