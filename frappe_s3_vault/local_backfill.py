import os
from urllib.parse import unquote
import frappe


def secure_url(file_id):
    return f"/api/method/frappe_s3_vault.api.download?file={file_id}"


def path_from_file_url(file_url):
    if not file_url:
        return None

    file_url = unquote(file_url)

    if file_url.startswith("/private/files/"):
        return frappe.get_site_path("private", "files", os.path.basename(file_url))

    if file_url.startswith("/files/"):
        return frappe.get_site_path("public", "files", os.path.basename(file_url))

    return None


def has_uploaded_vault_record(file_id):
    return bool(
        frappe.db.exists(
            "S3 Vault File",
            {
                "file": file_id,
                "status": "Uploaded",
            },
        )
    )


def force_finalize_file(file_id):
    file_doc = frappe.get_doc("File", file_id)

    # collect local paths before changing file_url
    paths = set()

    p = path_from_file_url(file_doc.file_url)
    if p:
        paths.add(p)

    vault_rows = frappe.get_all(
        "S3 Vault File",
        filters={"file": file_id, "status": "Uploaded"},
        fields=["name", "local_file_url", "object_key"],
        order_by="creation desc",
    )

    for row in vault_rows:
        p = path_from_file_url(row.local_file_url)
        if p:
            paths.add(p)

        if row.object_key:
            base = os.path.basename(row.object_key)
            paths.add(frappe.get_site_path("private", "files", base))
            paths.add(frappe.get_site_path("public", "files", base))

    deleted = []
    still_exists = []

    for p in paths:
        if p and os.path.isfile(p):
            try:
                os.remove(p)
                deleted.append(p)
            except Exception as e:
                still_exists.append(f"{p}: {e}")

    # verify
    for p in paths:
        if p and os.path.isfile(p):
            still_exists.append(p)

    frappe.db.set_value("File", file_id, "file_url", secure_url(file_id), update_modified=False)

    deleted_flag = 0 if still_exists else 1

    for row in vault_rows:
        frappe.db.set_value("S3 Vault File", row.name, "local_file_deleted", deleted_flag, update_modified=False)

    frappe.db.commit()

    return {
        "file": file_id,
        "final_url": secure_url(file_id),
        "deleted": deleted,
        "still_exists": still_exists,
        "local_file_deleted": deleted_flag,
    }


def upload_and_finalize_local_file(file_id):
    if not frappe.db.exists("File", file_id):
        return f"File not found: {file_id}"

    file_doc = frappe.get_doc("File", file_id)

    if not file_doc.file_url or not (
        file_doc.file_url.startswith("/private/files/") or file_doc.file_url.startswith("/files/")
    ):
        return f"Skipped {file_id}: not local file_url"

    path = path_from_file_url(file_doc.file_url)

    if not path or not os.path.isfile(path):
        return f"Skipped {file_id}: physical local file not found: {file_doc.file_url}"

    if not has_uploaded_vault_record(file_id):
        from frappe_s3_vault.handlers import upload_file_to_s3
        upload_file_to_s3(file_id)

    return force_finalize_file(file_id)


def repair_uploaded_files_with_local_url(limit=500):
    # Important: use LIKE value as SQL parameter, not literal %, to avoid Python % error.
    rows = frappe.db.sql(
        """
        select f.name
        from tabFile f
        where f.file_url like %s
           or f.file_url like %s
        order by f.creation desc
        limit %s
        """,
        ("/private/files/%", "/files/%", int(limit)),
        as_dict=True,
    )

    out = []

    for row in rows:
        try:
            file_doc = frappe.get_doc("File", row.name)

            # Only process files whose DocType has an enabled S3 Vault Rule.
            # This prevents deleting normal ERPNext files like logos/backups.
            from frappe_s3_vault.utils import enabled_rule_for_file
            rule = enabled_rule_for_file(file_doc)

            if not rule:
                continue

            out.append(str(upload_and_finalize_local_file(row.name)))

        except Exception:
            frappe.log_error(frappe.get_traceback(), "S3 Vault Local Backfill Failed")
            out.append(f"Failed: {row.name}")

    return "\n\n".join(out) if out else "No matching local files with enabled S3 Vault Rule found"
