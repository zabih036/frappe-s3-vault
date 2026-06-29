import os
import re
from urllib.parse import quote

import frappe

DOWNLOAD_PREFIX = "/api/method/frappe_s3_vault.api.download"


TEXT_FIELD_TYPES = {
    "Data",
    "Small Text",
    "Text",
    "Long Text",
    "Code",
    "Markdown Editor",
    "HTML",
    "JSON",
    "Read Only",
}


def secure_url_for_file(file_name):
    return f"{DOWNLOAD_PREFIX}?file={file_name}"


def get_file_and_vault(file_name):
    file_doc = frappe.get_doc("File", file_name)

    vault_name = frappe.db.get_value(
        "S3 Vault File",
        {"file": file_name, "status": "Uploaded"},
        "name",
        order_by="creation desc",
    )

    vault_doc = frappe.get_doc("S3 Vault File", vault_name) if vault_name else None
    return file_doc, vault_doc


def build_old_url_variants(file_doc, vault_doc=None):
    urls = set()

    for value in [
        file_doc.file_url,
        getattr(vault_doc, "file_url", None) if vault_doc else None,
        getattr(vault_doc, "local_file_url", None) if vault_doc else None,
    ]:
        if value:
            urls.add(value)
            urls.add(quote(value, safe="/:?=&"))

    # Add basename-based variants because Raven may store encoded full URL
    possible_paths = set()

    for value in list(urls):
        if "/private/files/" in value:
            possible_paths.add("/private/files/" + value.split("/private/files/", 1)[1])
        if "/files/" in value:
            possible_paths.add("/files/" + value.split("/files/", 1)[1])

    if file_doc.file_name:
        possible_paths.add("/private/files/" + file_doc.file_name)
        possible_paths.add("/private/files/" + quote(file_doc.file_name))
        possible_paths.add("/files/" + file_doc.file_name)
        possible_paths.add("/files/" + quote(file_doc.file_name))

    for path in possible_paths:
        urls.add(path)
        urls.add(quote(path, safe="/:?=&"))

    # escaped slash variants sometimes appear inside JSON strings
    for value in list(urls):
        urls.add(value.replace("/", "\\/"))

    return sorted(urls, key=len, reverse=True)


def replace_any_url(text, old_variants, new_url):
    if not isinstance(text, str):
        return text

    new_text = text

    for old in old_variants:
        if old:
            new_text = new_text.replace(old, new_url)

    # Replace absolute URLs like http://193.181.211.71/private/files/name.png
    for old in old_variants:
        if not old:
            continue

        if old.startswith("/private/files/") or old.startswith("/files/"):
            escaped = re.escape(old)
            new_text = re.sub(
                r"https?://[^\"'\s<>)]+%s" % escaped,
                new_url,
                new_text,
            )

            escaped_encoded = re.escape(quote(old, safe="/:?=&"))
            new_text = re.sub(
                r"https?://[^\"'\s<>)]+%s" % escaped_encoded,
                new_url,
                new_text,
            )

    return new_text


def update_doc_text_fields(doc, old_variants, new_url):
    changed = 0

    for df in doc.meta.fields:
        if df.fieldtype == "Table":
            children = doc.get(df.fieldname) or []
            for child in children:
                changed += update_doc_text_fields(child, old_variants, new_url)
            continue

        if df.fieldtype not in TEXT_FIELD_TYPES:
            continue

        value = doc.get(df.fieldname)
        if not isinstance(value, str):
            continue

        new_value = replace_any_url(value, old_variants, new_url)

        if new_value != value:
            doc.db_set(df.fieldname, new_value, update_modified=False)
            changed += 1

    return changed


def repair_one_file_url(file_name):
    file_doc, vault_doc = get_file_and_vault(file_name)

    if not vault_doc:
        return f"No uploaded S3 Vault File record for {file_name}"

    new_url = secure_url_for_file(file_doc.name)
    old_variants = build_old_url_variants(file_doc, vault_doc)

    # 1. Update tabFile.file_url
    if file_doc.file_url != new_url:
        frappe.db.set_value("File", file_doc.name, "file_url", new_url, update_modified=False)

    changed = 0

    # 2. Update attached document, example Raven Message
    if file_doc.attached_to_doctype and file_doc.attached_to_name:
        if frappe.db.exists(file_doc.attached_to_doctype, file_doc.attached_to_name):
            target = frappe.get_doc(file_doc.attached_to_doctype, file_doc.attached_to_name)
            changed += update_doc_text_fields(target, old_variants, new_url)

    frappe.db.commit()
    return f"Repaired {file_name}, changed_fields={changed}, new_url={new_url}"


def repair_all_uploaded_links(reference_doctype="Raven Message", limit=500):
    rows = frappe.get_all(
        "S3 Vault File",
        filters={"status": "Uploaded"},
        fields=["file"],
        order_by="creation desc",
        limit=limit,
    )

    fixed = 0

    for row in rows:
        if not row.file or not frappe.db.exists("File", row.file):
            continue

        file_doc = frappe.get_doc("File", row.file)

        if reference_doctype and file_doc.attached_to_doctype != reference_doctype:
            continue

        repair_one_file_url(row.file)
        fixed += 1

    return f"Fixed {fixed} uploaded file links"


def find_local_urls(doctype="Raven Message", limit=20):
    names = frappe.get_all(doctype, pluck="name", limit=limit, order_by="modified desc")
    found = []

    for name in names:
        doc = frappe.get_doc(doctype, name)

        for df in doc.meta.fields:
            if df.fieldtype not in TEXT_FIELD_TYPES:
                continue

            value = doc.get(df.fieldname)
            if isinstance(value, str) and ("/private/files/" in value or "/files/" in value):
                found.append({
                    "docname": name,
                    "fieldname": df.fieldname,
                    "value": value[:300],
                })

    return found
