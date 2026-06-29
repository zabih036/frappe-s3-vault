import os
import frappe

def get_setting_value(fieldnames, default=None):
    try:
        settings = frappe.get_single("S3 Vault Settings")
        for f in fieldnames:
            if hasattr(settings, f) and settings.get(f) not in (None, ""):
                return settings.get(f)
    except Exception:
        pass
    return default

def get_keep_days(rule=None):
    if rule:
        value = getattr(rule, "keep_local_copy_days", None)
        if value not in (None, ""):
            return int(value)

    return int(get_setting_value(
        ["keep_local_copy_days", "local_keep_days", "delete_local_after_days"],
        0
    ) or 0)

def should_delete_local(rule=None):
    if rule:
        value = getattr(rule, "delete_local_after_upload", None)
        if value not in (None, ""):
            return bool(value)

    return bool(get_setting_value(
        ["delete_local_after_upload_default", "delete_local_after_upload"],
        1
    ))

def path_from_file_url(file_url):
    if not file_url:
        return None

    file_url = file_url.split("?")[0]

    if file_url.startswith("/private/files/"):
        filename = os.path.basename(file_url)
        return frappe.get_site_path("private", "files", filename)

    if file_url.startswith("/files/"):
        filename = os.path.basename(file_url)
        return frappe.get_site_path("public", "files", filename)

    return None

def delete_local_file(path):
    if path and os.path.exists(path):
        os.remove(path)
        return True
    return False
