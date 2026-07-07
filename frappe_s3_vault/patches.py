import frappe

def fix_autoname():
    for dt in ["S3 Vault File", "S3 Vault Log"]:
        if frappe.db.exists("DocType", dt):
            frappe.db.set_value("DocType", dt, "autoname", "hash", update_modified=False)

    if frappe.db.exists("S3 Vault File", "S3-VFILE-.#####"):
        frappe.rename_doc(
            "S3 Vault File",
            "S3-VFILE-.#####",
            "S3VFILE-OLD-" + frappe.generate_hash(length=8),
            force=True,
            ignore_permissions=True
        )

    if frappe.db.exists("S3 Vault Log", "S3-VLOG-.#####"):
        frappe.rename_doc(
            "S3 Vault Log",
            "S3-VLOG-.#####",
            "S3VLOG-OLD-" + frappe.generate_hash(length=8),
            force=True,
            ignore_permissions=True
        )

    frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.
    frappe.clear_cache()
    return "Fixed S3 Vault File and S3 Vault Log autoname"
