import os
from urllib.parse import unquote
import frappe


def path_from_url(file_url):
    if not file_url:
        return None

    file_url = unquote(file_url)

    if file_url.startswith("/private/files/"):
        return frappe.get_site_path("private", "files", os.path.basename(file_url))

    if file_url.startswith("/files/"):
        return frappe.get_site_path("public", "files", os.path.basename(file_url))

    return None


def local_url_still_needed(local_url):
    if not local_url:
        return False

    # Local URL is still needed only if some File row still points to it
    # and that File has no uploaded S3 Vault File record.
    rows = frappe.db.sql(
        """
        select f.name
        from tabFile f
        left join `tabS3 Vault File` vf
            on vf.file = f.name and vf.status = 'Uploaded'
        where f.file_url = %s
          and vf.name is null
        limit 1
        """,
        local_url,
        as_dict=True,
    )

    return bool(rows)


def cleanup_uploaded_local_files(limit=500):
    rows = frappe.get_all(
        "S3 Vault File",
        filters={"status": "Uploaded"},
        fields=["name", "file", "local_file_url", "local_file_deleted"],
        order_by="creation desc",
        limit=limit,
    )

    deleted = 0
    skipped = 0

    for row in rows:
        if row.local_file_deleted:
            continue

        local_url = row.local_file_url
        path = path_from_url(local_url)

        if not path:
            skipped += 1
            continue

        if local_url_still_needed(local_url):
            skipped += 1
            continue

        if os.path.exists(path):
            os.remove(path)
            deleted += 1

        frappe.db.set_value(
            "S3 Vault File",
            row.name,
            "local_file_deleted",
            1,
            update_modified=False,
        )

    frappe.db.commit()
    return f"Deleted local files={deleted}, skipped={skipped}"


def deduplicate_vault_files():
    files = frappe.db.sql(
        """
        select file
        from `tabS3 Vault File`
        where ifnull(file, '') != ''
        group by file
        having count(*) > 1
        """,
        as_dict=True,
    )

    fixed = 0

    for r in files:
        records = frappe.get_all(
            "S3 Vault File",
            filters={"file": r.file},
            fields=["name", "local_file_deleted", "creation"],
            order_by="creation desc",
        )

        keep = records[0]
        delete_these = records[1:]

        any_deleted = any(int(x.local_file_deleted or 0) == 1 for x in records)

        if any_deleted:
            frappe.db.set_value(
                "S3 Vault File",
                keep.name,
                "local_file_deleted",
                1,
                update_modified=False,
            )

        for old in delete_these:
            frappe.delete_doc("S3 Vault File", old.name, force=True, ignore_permissions=True)

        fixed += 1

    frappe.db.commit()
    return f"Deduplicated files={fixed}"


def add_unique_file_index():
    try:
        frappe.db.sql(
            """
            alter table `tabS3 Vault File`
            add unique key unique_s3_vault_file_file (`file`)
            """
        )
        frappe.db.commit()
        return "Unique index added"
    except Exception as e:
        return f"Unique index skipped/exists: {str(e)}"


def run_cleanup():
    a = deduplicate_vault_files()
    b = cleanup_uploaded_local_files()
    c = add_unique_file_index()
    return f"{a}\n{b}\n{c}"

def delete_local_by_download_url(download_url):
    import os
    from urllib.parse import urlparse, parse_qs, unquote
    import frappe

    qs = parse_qs(urlparse(download_url).query)
    file_id = (qs.get("file") or [None])[0]

    if not file_id:
        return "No file id found in URL"

    rows = frappe.get_all(
        "S3 Vault File",
        filters={"file": file_id, "status": "Uploaded"},
        fields=["name", "local_file_url"],
        order_by="creation desc"
    )

    if not rows:
        return f"No uploaded S3 Vault File found for {file_id}"

    deleted = 0

    for row in rows:
        local_url = row.local_file_url
        if not local_url:
            continue

        local_url = unquote(local_url)

        if local_url.startswith("/private/files/"):
            path = frappe.get_site_path("private", "files", os.path.basename(local_url))
        elif local_url.startswith("/files/"):
            path = frappe.get_site_path("public", "files", os.path.basename(local_url))
        else:
            continue

        if os.path.exists(path):
            os.remove(path)
            deleted += 1

        frappe.db.set_value(
            "S3 Vault File",
            row.name,
            "local_file_deleted",
            1,
            update_modified=False
        )

    frappe.db.commit()
    return f"File ID {file_id}: deleted local files={deleted}"

def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def get_local_path_from_url(local_url):
    import os
    from urllib.parse import unquote
    import frappe

    if not local_url:
        return None

    local_url = unquote(local_url)

    if local_url.startswith("/private/files/"):
        return frappe.get_site_path("private", "files", os.path.basename(local_url))

    if local_url.startswith("/files/"):
        return frappe.get_site_path("public", "files", os.path.basename(local_url))

    return None


def delete_local_by_file_id(file_id):
    import os
    import glob
    import frappe

    if not frappe.db.exists("File", file_id):
        return f"File not found: {file_id}"

    vault = frappe.db.get_value(
        "S3 Vault File",
        {"file": file_id, "status": "Uploaded"},
        ["name", "local_file_url", "file_hash"],
        as_dict=True,
        order_by="creation desc"
    )

    if not vault:
        return f"No uploaded S3 Vault File record found for File ID: {file_id}"

    candidates = set()

    # 1. exact stored local path
    exact_path = get_local_path_from_url(vault.local_file_url)
    if exact_path:
        candidates.add(exact_path)

    # 2. search by hash in private/public files
    search_dirs = [
        frappe.get_site_path("private", "files"),
        frappe.get_site_path("public", "files"),
    ]

    if vault.file_hash:
        for folder in search_dirs:
            for path in glob.glob(os.path.join(folder, "*")):
                if os.path.isfile(path):
                    try:
                        if sha256_file(path) == vault.file_hash:
                            candidates.add(path)
                    except Exception:
                        pass

    deleted = []

    for path in candidates:
        if path and os.path.exists(path):
            os.remove(path)
            deleted.append(path)

    if deleted:
        frappe.db.set_value(
            "S3 Vault File",
            vault.name,
            "local_file_deleted",
            1,
            update_modified=False
        )
        frappe.db.commit()
        return "Deleted local files:\n" + "\n".join(deleted)

    return f"No local file found to delete for File ID: {file_id}"


def delete_local_by_download_url(download_url):
    from urllib.parse import urlparse, parse_qs

    file_id = (parse_qs(urlparse(download_url).query).get("file") or [None])[0]

    if not file_id:
        return "No file id found in download URL"

    return delete_local_by_file_id(file_id)

# stronger cleanup: find local file by File ID, original filename, object_key basename, and hash
def delete_local_by_file_id_strong(file_id):
    import os
    import glob
    import hashlib
    from urllib.parse import unquote
    import frappe

    def sha256_file(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def add_candidate(candidates, path):
        if path:
            candidates.add(path)
            candidates.add(unquote(path))

    if not frappe.db.exists("File", file_id):
        return f"File not found: {file_id}"

    file_doc = frappe.get_doc("File", file_id)

    vault = frappe.db.get_value(
        "S3 Vault File",
        {"file": file_id, "status": "Uploaded"},
        ["name", "local_file_url", "object_key", "file_hash"],
        as_dict=True,
        order_by="creation desc"
    )

    if not vault:
        return f"No uploaded S3 Vault File found for File ID: {file_id}"

    private_dir = frappe.get_site_path("private", "files")
    public_dir = frappe.get_site_path("public", "files")

    candidates = set()

    # 1. Stored original local URL
    if vault.local_file_url:
        local_url = unquote(vault.local_file_url)

        if local_url.startswith("/private/files/"):
            add_candidate(candidates, os.path.join(private_dir, os.path.basename(local_url)))

        if local_url.startswith("/files/"):
            add_candidate(candidates, os.path.join(public_dir, os.path.basename(local_url)))

    # 2. Current Frappe File name, example Account.pdf
    if file_doc.file_name:
        add_candidate(candidates, os.path.join(private_dir, file_doc.file_name))
        add_candidate(candidates, os.path.join(public_dir, file_doc.file_name))

    # 3. Object key basename, example 93b4a95c24_Account.pdf
    if vault.object_key:
        object_base = os.path.basename(vault.object_key)
        add_candidate(candidates, os.path.join(private_dir, object_base))
        add_candidate(candidates, os.path.join(public_dir, object_base))

        # Extract original name from 93b4a95c24_Account.pdf => Account.pdf
        prefix = file_id + "_"
        if object_base.startswith(prefix):
            original_from_key = object_base[len(prefix):]
            add_candidate(candidates, os.path.join(private_dir, original_from_key))
            add_candidate(candidates, os.path.join(public_dir, original_from_key))

    # 4. New planned renamed style: 93b4a95c24.pdf
    ext = os.path.splitext(file_doc.file_name or "")[1]
    if not ext and vault.object_key:
        ext = os.path.splitext(vault.object_key)[1]

    if ext:
        add_candidate(candidates, os.path.join(private_dir, file_id + ext))
        add_candidate(candidates, os.path.join(public_dir, file_id + ext))

    # 5. Frappe duplicate renamed files: Account47cab1.pdf, Account-1.pdf, etc.
    if file_doc.file_name:
        base, ext2 = os.path.splitext(file_doc.file_name)
        for folder in [private_dir, public_dir]:
            for p in glob.glob(os.path.join(folder, base + "*" + ext2)):
                add_candidate(candidates, p)

    # 6. File-ID based patterns
    for folder in [private_dir, public_dir]:
        for p in glob.glob(os.path.join(folder, file_id + "*")):
            add_candidate(candidates, p)

    existing = [p for p in candidates if p and os.path.exists(p) and os.path.isfile(p)]

    if not existing:
        return (
            f"No local file found for File ID: {file_id}\n"
            f"Checked File Name: {file_doc.file_name}\n"
            f"Checked local_file_url: {vault.local_file_url}\n"
            f"Checked object_key: {vault.object_key}"
        )

    deleted = []

    # If hash exists, delete only matching hash files
    if vault.file_hash:
        for p in existing:
            try:
                if sha256_file(p) == vault.file_hash:
                    os.remove(p)
                    deleted.append(p)
            except Exception:
                pass
    else:
        # If no hash, delete only exact/high-confidence matches
        for p in existing:
            filename = os.path.basename(p)
            high_confidence = (
                filename == file_doc.file_name
                or filename == os.path.basename(vault.local_file_url or "")
                or filename == os.path.basename(vault.object_key or "")
                or filename.startswith(file_id)
            )

            if high_confidence:
                os.remove(p)
                deleted.append(p)

    if deleted:
        frappe.db.set_value(
            "S3 Vault File",
            vault.name,
            "local_file_deleted",
            1,
            update_modified=False
        )
        frappe.db.commit()

        return "Deleted local files:\n" + "\n".join(deleted)

    return (
        f"Found candidates but did not delete because hash/high-confidence check failed:\n"
        + "\n".join(existing)
    )

def inspect_and_delete_local_file(file_id, force=0):
    import os
    import glob
    import hashlib
    from urllib.parse import unquote
    import frappe

    force = int(force or 0)

    def sha256_file(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def add(candidates, path, reason):
        if path:
            candidates[path] = reason
            candidates[unquote(path)] = reason + " decoded"

    def path_from_local_url(local_url):
        if not local_url:
            return None

        local_url = unquote(local_url)

        if local_url.startswith("/private/files/"):
            return frappe.get_site_path("private", "files", os.path.basename(local_url))

        if local_url.startswith("/files/"):
            return frappe.get_site_path("public", "files", os.path.basename(local_url))

        return None

    if not frappe.db.exists("File", file_id):
        return f"File not found: {file_id}"

    file_doc = frappe.get_doc("File", file_id)

    vault_rows = frappe.get_all(
        "S3 Vault File",
        filters={"file": file_id},
        fields=["name", "status", "local_file_url", "object_key", "file_hash", "local_file_deleted"],
        order_by="creation desc",
    )

    if not vault_rows:
        return f"No S3 Vault File record found for File ID: {file_id}"

    private_dir = frappe.get_site_path("private", "files")
    public_dir = frappe.get_site_path("public", "files")

    candidates = {}

    # 1. From all vault local_file_url values
    for v in vault_rows:
        p = path_from_local_url(v.local_file_url)
        add(candidates, p, f"vault local_file_url from {v.name}")

    # 2. From current File.file_url if still local
    p = path_from_local_url(file_doc.file_url)
    add(candidates, p, "current File.file_url")

    # 3. From File.file_name
    if file_doc.file_name:
        add(candidates, os.path.join(private_dir, file_doc.file_name), "File.file_name private")
        add(candidates, os.path.join(public_dir, file_doc.file_name), "File.file_name public")

    # 4. From object_key basename and stripped basename
    for v in vault_rows:
        if v.object_key:
            object_base = os.path.basename(v.object_key)
            add(candidates, os.path.join(private_dir, object_base), "object_key basename private")
            add(candidates, os.path.join(public_dir, object_base), "object_key basename public")

            prefix = file_id + "_"
            if object_base.startswith(prefix):
                original = object_base[len(prefix):]
                add(candidates, os.path.join(private_dir, original), "object_key stripped private")
                add(candidates, os.path.join(public_dir, original), "object_key stripped public")

    # 5. Glob search: Account.pdf may become Account47cab1.pdf
    if file_doc.file_name:
        base, ext = os.path.splitext(file_doc.file_name)
        for folder in [private_dir, public_dir]:
            for p in glob.glob(os.path.join(folder, base + "*" + ext)):
                add(candidates, p, "glob original filename")

    # 6. Glob search by file id
    for folder in [private_dir, public_dir]:
        for p in glob.glob(os.path.join(folder, file_id + "*")):
            add(candidates, p, "glob file id")

    existing = []
    for p, reason in candidates.items():
        if p and os.path.exists(p) and os.path.isfile(p):
            existing.append((p, reason))

    report = []
    report.append(f"File ID: {file_id}")
    report.append(f"File.file_name: {file_doc.file_name}")
    report.append(f"File.file_url: {file_doc.file_url}")
    report.append("Vault records:")
    for v in vault_rows:
        report.append(f"- {v.name} status={v.status} local_file_url={v.local_file_url} object_key={v.object_key} deleted={v.local_file_deleted}")

    report.append("Existing candidates:")
    for p, reason in existing:
        report.append(f"- {p} [{reason}]")

    if not existing:
        return "\n".join(report) + "\n\nRESULT: No matching local file exists on disk."

    # Use hash only if available and matches. If no hash match, force=1 can still delete candidates.
    hashes = [v.file_hash for v in vault_rows if v.file_hash]
    deleted = []
    skipped = []

    for p, reason in existing:
        should_delete = False

        if hashes:
            try:
                current_hash = sha256_file(p)
                if current_hash in hashes:
                    should_delete = True
                elif force:
                    should_delete = True
                else:
                    skipped.append(f"{p} hash mismatch")
            except Exception as e:
                skipped.append(f"{p} hash check failed: {e}")
        else:
            # no hash stored, delete only with force
            should_delete = bool(force)

        if should_delete and os.path.exists(p):
            os.remove(p)
            deleted.append(p)

    if deleted:
        for v in vault_rows:
            frappe.db.set_value("S3 Vault File", v.name, "local_file_deleted", 1, update_modified=False)
        frappe.db.commit()

    report.append("Deleted:")
    for p in deleted:
        report.append(f"- {p}")

    report.append("Skipped:")
    for s in skipped:
        report.append(f"- {s}")

    if not deleted and not force:
        report.append("\nRESULT: Found local file but did not delete because hash did not match. Run again with force=1.")

    return "\n".join(report)
