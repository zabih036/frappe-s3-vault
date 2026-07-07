import frappe


UPLOAD_METHOD = "frappe_s3_vault.upload.upload_file_to_s3"
DOWNLOAD_PREFIX = "/api/method/frappe_s3_vault.api.download"

# True = no refresh bug, because upload finishes before browser gets response.
# False = faster UI, but file may show local URL until refresh.
SYNC_UPLOAD = False


def is_s3_url(file_url):
    return bool(file_url and str(file_url).startswith(DOWNLOAD_PREFIX))


def get_db_file_url(file_id):
    try:
        return frappe.db.get_value("File", file_id, "file_url")
    except Exception:
        return None


def has_existing_vault_record(file_id):
    try:
        return bool(
            frappe.db.exists(
                "S3 Vault File",
                {
                    "file": file_id,
                    "status": ["in", ["Uploaded", "Deleted", "Missing"]],
                },
            )
        )
    except Exception:
        return False


def should_skip_upload(file_doc):
    if not file_doc:
        return True

    if getattr(frappe.flags, "s3_vault_skip_upload_hook", False):
        return True

    if is_s3_url(file_doc.file_url):
        return True

    db_file_url = get_db_file_url(file_doc.name)

    if is_s3_url(db_file_url):
        return True

    if has_existing_vault_record(file_doc.name):
        return True

    return False


def file_has_rule(file_doc):
    try:
        from frappe_s3_vault.utils import enabled_rule_for_file
        return bool(enabled_rule_for_file(file_doc))
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Rule Check Failed")
        return False


def run_s3_upload_now(file_doc):
    """
    Synchronous upload.

    This fixes the refresh problem for all DocTypes because tabFile.file_url
    is changed before the upload request returns to the browser.
    """

    try:
        if should_skip_upload(file_doc):
            return

        if not file_has_rule(file_doc):
            return

        from frappe_s3_vault.upload import upload_file_to_s3

        frappe.flags.s3_vault_skip_upload_hook = True

        try:
            upload_file_to_s3(file_doc.name)
        finally:
            frappe.flags.s3_vault_skip_upload_hook = False

    except Exception:
        frappe.flags.s3_vault_skip_upload_hook = False
        frappe.log_error(frappe.get_traceback(), "S3 Vault Sync Upload Failed")
        raise


def enqueue_s3_upload(file_doc, method=None):
    """
    Backward-compatible name.

    In production we now use synchronous upload to prevent local URL showing
    until browser refresh.
    """

    try:
        if should_skip_upload(file_doc):
            return

        if not file_has_rule(file_doc):
            return

        if SYNC_UPLOAD:
            run_s3_upload_now(file_doc)
            return

        frappe.enqueue(
            UPLOAD_METHOD,
            queue="long",
            file_name=file_doc.name,
            enqueue_after_commit=True,
            job_id=f"s3-vault-upload-{file_doc.name}",
            deduplicate=True,
        )

    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Upload Hook Failed")
        raise


def after_insert_file(doc, method=None):
    enqueue_s3_upload(doc)


def on_update_file(doc, method=None):
    enqueue_s3_upload(doc)


def upload_file_to_s3(file_name):
    """
    Backward-compatible wrapper for old queued jobs.
    """

    from frappe_s3_vault.upload import upload_file_to_s3 as clean_upload
    return clean_upload(file_name)


def get_file_storage_rows(file_id):
    return frappe.get_all(
        "S3 Vault File",
        filters={"file": file_id},
        fields=[
            "name",
            "bucket",
            "bucket_name",
            "object_key",
            "status",
            "attached_to_doctype",
            "attached_to_name",
        ],
        order_by="creation desc",
    )


def delete_s3_objects_for_file(file_id):
    from frappe_s3_vault.utils import s3_client
    from frappe_s3_vault.records import mark_deleted_from_storage

    rows = get_file_storage_rows(file_id)

    results = []

    for row in rows:
        item = {
            "storage_file": row.name,
            "bucket": row.bucket,
            "bucket_name": row.bucket_name,
            "object_key": row.object_key,
            "status": "Skipped",
            "error": None,
        }

        if not row.object_key or not row.bucket:
            item["status"] = "Skipped"
            item["error"] = "Missing bucket or object_key"
            results.append(item)
            continue

        try:
            bucket_doc = frappe.get_doc("S3 Vault Bucket", row.bucket)
            bucket_name = row.bucket_name or bucket_doc.bucket_name
            item["bucket_name"] = bucket_name

            client = s3_client(bucket_doc)

            try:
                client.delete_object(
                    Bucket=bucket_name,
                    Key=row.object_key,
                )
                item["status"] = "Deleted"

            except Exception:
                item["status"] = "Deleted"
                item["error"] = "Object may already be missing in storage"

            mark_deleted_from_storage(file_id, release_file_link=False)

        except Exception:
            item["status"] = "Failed"
            item["error"] = frappe.get_traceback()
            frappe.log_error(frappe.get_traceback(), "S3 Vault Delete Object Failed")

        results.append(item)

    return results


def clean_raven_deleted_file(file_id):
    try:
        from frappe_s3_vault.delete_fix import clean_raven_message_for_deleted_file
        clean_raven_message_for_deleted_file(file_id)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Raven Delete Cleanup Failed")


def write_delete_logs(file_id, delete_results):
    from frappe_s3_vault.logs import write_log

    if not delete_results:
        write_log(
            action="Delete",
            status="Skipped",
            file_id=file_id,
            error_message="No S3 Vault File rows found for deleted File",
            commit=False,
        )
        return

    for item in delete_results:
        status = "Success" if item["status"] == "Deleted" else "Failed"

        if item["status"] == "Skipped":
            status = "Failed"

        message = item.get("error") or f"Deleted object {item.get('object_key')} from storage"

        write_log(
            action="Delete",
            status=status,
            file_id=file_id,
            storage_file=item.get("storage_file"),
            bucket_name=item.get("bucket_name"),
            object_key=item.get("object_key"),
            error_message=message,
            commit=False,
        )


def release_links_after_delete(file_id):
    try:
        from frappe_s3_vault.records import release_file_link_from_vault
        release_file_link_from_vault(file_id)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "S3 Vault Release File Link Failed")

    try:
        frappe.db.sql(
            "update `tabS3 Vault Log` set file=NULL where file=%s and action='Delete'",
            file_id,
        )
    except Exception:
        try:
            frappe.db.sql(
                "update `tabS3 Vault Log` set file='' where file=%s and action='Delete'",
                file_id,
            )
        except Exception:
            pass


def on_trash_file(doc, method=None):
    if getattr(frappe.flags, "s3_vault_skip_file_delete_hook", False):
        return

    file_id = doc.name

    try:
        delete_results = delete_s3_objects_for_file(file_id)

        clean_raven_deleted_file(file_id)

        write_delete_logs(file_id, delete_results)

        release_links_after_delete(file_id)

        frappe.db.commit()  # nosemgrep: frappe-manual-commit - explicit commit is intentional for cleanup/background compatibility.

    except Exception:
        try:
            from frappe_s3_vault.logs import write_exception_log
            write_exception_log(
                action="Delete",
                file_id=file_id,
                error_message="File delete failed",
                commit=True,
            )
        except Exception:
            pass

        frappe.log_error(frappe.get_traceback(), "S3 Vault File Delete Failed")


# stable override: normal Frappe File delete cleanup for S3 Vault
def on_trash_file(doc, method=None):
    import frappe
    from frappe.utils import now_datetime, cint

    if getattr(frappe.flags, "s3_vault_skip_file_delete_hook", False):
        return

    rows = frappe.get_all(
        "S3 Vault File",
        filters={
            "file": doc.name,
            "status": ["in", ["Uploaded", "Failed", "Missing", "Soft Deleted", "Deleted"]],
        },
        fields=[
            "name",
            "file",
            "status",
            "bucket",
            "bucket_name",
            "object_key",
            "rule_used",
            "attached_to_doctype",
            "attached_to_name",
        ],
        order_by="creation desc",
    )

    if not rows:
        return

    from frappe_s3_vault.utils import s3_client
    from frappe_s3_vault.logs import write_log

    for row in rows:
        deleted_from_storage = 0
        final_status = "Deleted"
        message = None

        try:
            allow_delete = 1

            if row.rule_used and frappe.db.exists("S3 Vault Rule", row.rule_used):
                rule = frappe.get_doc("S3 Vault Rule", row.rule_used)
                allow_delete = cint(getattr(rule, "allow_delete_from_s3", 0))

            if allow_delete:
                if row.bucket and row.object_key:
                    bucket_doc = frappe.get_doc("S3 Vault Bucket", row.bucket)
                    bucket_name = row.bucket_name or bucket_doc.bucket_name

                    client = s3_client(bucket_doc)

                    try:
                        client.delete_object(
                            Bucket=bucket_name,
                            Key=row.object_key,
                        )
                        deleted_from_storage = 1
                        message = f"Deleted object {row.object_key} from storage"
                    except Exception:
                        deleted_from_storage = 1
                        message = "Delete requested; object may already be missing in S3/Wasabi"
                else:
                    message = "Skipped S3 delete because bucket or object_key is missing"

            else:
                final_status = "Soft Deleted"
                deleted_from_storage = 0
                message = f"S3 object preserved because rule {row.rule_used} does not allow delete from S3"

            frappe.db.set_value(
                "S3 Vault File",
                row.name,
                {
                    "file": None,
                    "status": final_status,
                    "deleted_from_storage": deleted_from_storage,
                    "deleted_on": now_datetime(),
                    "deleted_by": frappe.session.user,
                },
                update_modified=False,
            )

            # Important: prevent File deletion being blocked by S3 Vault Log.file
            frappe.db.sql(
                """
                update `tabS3 Vault Log`
                set file=NULL
                where file=%s
                """,
                doc.name,
            )

            write_log(
                action="Delete",
                status="Success",
                file_id=None,
                storage_file=row.name,
                bucket_name=row.bucket_name,
                object_key=row.object_key,
                error_message=message,
                commit=False,
            )

        except Exception:
            frappe.log_error(frappe.get_traceback(), "S3 Vault Normal File Delete Failed")

            try:
                write_log(
                    action="Delete",
                    status="Failed",
                    file_id=None,
                    storage_file=row.name,
                    bucket_name=row.bucket_name,
                    object_key=row.object_key,
                    error_message="Normal File delete cleanup failed",
                    traceback_text=frappe.get_traceback(),
                    commit=False,
                )
            except Exception:
                pass

            raise
