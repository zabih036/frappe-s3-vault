app_name = "frappe_s3_vault"
app_title = "Frappe S3 Vault"
app_publisher = "AOGC"
app_description = "Simple S3/Wasabi attachment storage for selected Frappe DocTypes"
app_email = "admin@example.com"
app_license = "MIT"
required_apps = []


doc_events = {
    "File": {
        "after_insert": "frappe_s3_vault.handlers.after_insert_file",
        "on_update": "frappe_s3_vault.handlers.on_update_file",
        "on_trash": "frappe_s3_vault.handlers.on_trash_file",
    }
}


doctype_list_js = {
    "S3 Vault Log": "public/js/s3_vault_log_list.js",
}


# S3 Vault: catch Raven UI delete/update because Raven may not trigger File.on_trash.
doc_events.update(
    {
        "Raven Message": {
            "on_update": "frappe_s3_vault.raven_delete_hooks.on_update_raven_message",
            "on_trash": "frappe_s3_vault.raven_delete_hooks.on_trash_raven_message",
        }
    }
)


# S3 Vault: refresh normal Desk forms when async upload changes File.file_url.
try:
    app_include_js
except NameError:
    app_include_js = []

if "/assets/frappe_s3_vault/js/s3_vault_realtime.js" not in app_include_js:
    app_include_js.append("/assets/frappe_s3_vault/js/s3_vault_realtime.js")


# S3 Vault: prepare Raven file/image messages before save.
try:
    doc_events
except NameError:
    doc_events = {}

doc_events.setdefault("Raven Message", {})
doc_events["Raven Message"][
    "validate"
] = "frappe_s3_vault.raven_message_hooks.prepare_raven_file_before_save"
doc_events["Raven Message"][
    "before_insert"
] = "frappe_s3_vault.raven_message_hooks.prepare_raven_file_before_save"

# Keep Raven delete cleanup hooks active.
doc_events["Raven Message"][
    "on_update"
] = "frappe_s3_vault.raven_delete_hooks.on_update_raven_message"
doc_events["Raven Message"][
    "on_trash"
] = "frappe_s3_vault.raven_delete_hooks.on_trash_raven_message"


# Allow File deletion even when referenced by S3 Vault audit/storage records.
# Cleanup is handled by File on_trash hooks.
try:
    ignore_links_on_delete
except NameError:
    ignore_links_on_delete = []

for _doctype in ["S3 Vault File", "S3 Vault Log"]:
    if _doctype not in ignore_links_on_delete:
        ignore_links_on_delete.append(_doctype)


# File-manager maintenance jobs.
scheduler_events = {
    "hourly": [
        "frappe_s3_vault.file_manager_jobs.cleanup_expired_archives",
        "frappe_s3_vault.file_manager_multipart.cleanup_expired_multipart_uploads",
        "frappe_s3_vault.file_manager_index.enqueue_due_index_syncs",
    ]
}


# Phase 3 setup: standard file-manager roles.
before_install = "frappe_s3_vault.setup.before_install"
before_migrate = "frappe_s3_vault.setup.before_migrate"
after_install = "frappe_s3_vault.setup.after_install"
after_migrate = "frappe_s3_vault.setup.after_migrate"

# Phase 3 record-level privacy for operation and multipart session records.
permission_query_conditions = {
    "S3 Vault Operation": "frappe_s3_vault.file_manager_permissions.operation_query_conditions",
    "S3 Vault Multipart Upload": "frappe_s3_vault.file_manager_permissions.multipart_query_conditions",
}

has_permission = {
    "S3 Vault Operation": "frappe_s3_vault.file_manager_permissions.operation_has_permission",
    "S3 Vault Multipart Upload": "frappe_s3_vault.file_manager_permissions.multipart_has_permission",
}
