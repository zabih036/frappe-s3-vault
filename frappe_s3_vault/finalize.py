import os
from urllib.parse import unquote
import frappe


def secure_url(file_id):
    return f"/api/method/frappe_s3_vault.api.download?file={file_id}"


def is_local_url(url):
    return bool(url and (url.startswith("/private/files/") or url.startswith("/files/")))


def path_from_url(url):
    if not url:
        return None

    url = unquote(url)

    if url.startswith("/private/files/"):
        return frappe.get_site_path("private", "files", os.path.basename(url))

    if url.startswith("/files/"):
        return frappe.get_site_path("public", "files", os.path.basename(url))

    return None


def finalize_uploaded_file(file_id):
    if not frappe.db.exists("File", file_id):
        return f"File not found: {file_id}"

    file_doc = frappe.get_doc("File", file_id)

    vault_rows = frappe.get_all(
        "S3 Vault File",
        filters={"file": file_id, "status": "Uploaded"},
        fields=["name", "local_file_url", "object_key"],
        order_by="creation desc",
    )

    if not vault_rows:
        return f"No uploaded S3 Vault File found for {file_id}"

    candidates = set()

    # Current tabFile.file_url, e.g. /private/files/Account.pdf
    if is_local_url(file_doc.file_url):
        p = path_from_url(file_doc.file_url)
        if p:
            candidates.add(p)

    # Stored original/renamed local URL
    for row in vault_rows:
        if is_local_url(row.local_file_url):
            p = path_from_url(row.local_file_url)
            if p:
                candidates.add(p)

        # Object key basename, e.g. 388497874d_Account.pdf
        if row.object_key:
            base = os.path.basename(row.object_key)
            candidates.add(frappe.get_site_path("private", "files", base))
            candidates.add(frappe.get_site_path("public", "files", base))

    deleted = []
    still_exists = []

    for p in candidates:
        if p and os.path.isfile(p):
            try:
                os.remove(p)
                deleted.append(p)
            except Exception as e:
                still_exists.append(f"{p} -> {e}")

    # Verify after delete
    for p in candidates:
        if p and os.path.isfile(p):
            still_exists.append(p)

    final_url = secure_url(file_id)

    # Always force File DocType URL back to API URL
    frappe.db.set_value("File", file_id, "file_url", final_url, update_modified=False)

    deleted_flag = 0 if still_exists else 1

    for row in vault_rows:
        frappe.db.set_value("S3 Vault File", row.name, "local_file_deleted", deleted_flag, update_modified=False)

    frappe.db.commit()

    return (
        f"File ID: {file_id}\n"
        f"Final File.file_url: {final_url}\n"
        f"Deleted: {deleted}\n"
        f"Still exists/errors: {still_exists}\n"
        f"local_file_deleted: {deleted_flag}"
    )


def finalize_uploaded_files_with_local_url(limit=500):
    rows = frappe.db.sql(
        """
        select distinct f.name
        from tabFile f
        inner join `tabS3 Vault File` vf
            on vf.file = f.name
           and vf.status = 'Uploaded'
        where f.file_url like '/private/files/%'
           or f.file_url like '/files/%'
        order by f.creation desc
        limit %s
        """,
        int(limit),
        as_dict=True,
    )

    out = []

    for row in rows:
        out.append(finalize_uploaded_file(row.name))

    return "\n\n".join(out) if out else "No uploaded S3 files with local File.file_url found"
