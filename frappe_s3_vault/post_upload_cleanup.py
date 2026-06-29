import os
from urllib.parse import unquote
import frappe


def _site_path_from_url(file_url):
    if not file_url:
        return None

    file_url = unquote(file_url)

    if file_url.startswith("/private/files/"):
        return frappe.get_site_path("private", "files", os.path.basename(file_url))

    if file_url.startswith("/files/"):
        return frappe.get_site_path("public", "files", os.path.basename(file_url))

    return None


def _candidate_paths(file_id):
    paths = set()

    file_doc = frappe.get_doc("File", file_id)

    rows = frappe.get_all(
        "S3 Vault File",
        filters={"file": file_id, "status": "Uploaded"},
        fields=["name", "local_file_url", "object_key"],
        order_by="creation desc",
    )

    # From S3 Vault File.local_file_url
    for r in rows:
        p = _site_path_from_url(r.local_file_url)
        if p:
            paths.add(p)
            paths.add(unquote(p))

    # From Wasabi object_key basename
    for r in rows:
        if r.object_key:
            base = os.path.basename(r.object_key)
            paths.add(frappe.get_site_path("private", "files", base))
            paths.add(frappe.get_site_path("public", "files", base))

    # From current File.file_url if still local
    p = _site_path_from_url(file_doc.file_url)
    if p:
        paths.add(p)
        paths.add(unquote(p))

    return list(paths)


def delete_local_copy_verified(file_id):
    if not frappe.db.exists("File", file_id):
        return f"File not found: {file_id}"

    rows = frappe.get_all(
        "S3 Vault File",
        filters={"file": file_id, "status": "Uploaded"},
        fields=["name", "local_file_url", "object_key", "local_file_deleted"],
        order_by="creation desc",
    )

    if not rows:
        return f"No uploaded S3 Vault File found for {file_id}"

    paths = _candidate_paths(file_id)

    deleted = []
    still_exists = []

    for p in paths:
        if p and os.path.isfile(p):
            try:
                os.remove(p)
                deleted.append(p)
            except Exception as e:
                still_exists.append(f"{p} -> {e}")

    # Verify again after delete
    for p in paths:
        if p and os.path.isfile(p):
            still_exists.append(p)

    final_deleted_flag = 0 if still_exists else 1

    for r in rows:
        frappe.db.set_value(
            "S3 Vault File",
            r.name,
            "local_file_deleted",
            final_deleted_flag,
            update_modified=False,
        )

    frappe.db.commit()

    return (
        f"File ID: {file_id}\n"
        f"Deleted files: {deleted}\n"
        f"Still exists/errors: {still_exists}\n"
        f"local_file_deleted set to: {final_deleted_flag}"
    )


def cleanup_all_verified(limit=500):
    rows = frappe.get_all(
        "S3 Vault File",
        filters={"status": "Uploaded"},
        fields=["file"],
        order_by="creation desc",
        limit=limit,
    )

    seen = set()
    output = []

    for r in rows:
        if not r.file or r.file in seen:
            continue

        seen.add(r.file)
        output.append(delete_local_copy_verified(r.file))

    return "\n\n".join(output)
