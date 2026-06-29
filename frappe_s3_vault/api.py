import frappe


def safe_log(action, status="Success", file_id=None, message=None):
    try:
        doc = frappe.new_doc("S3 Vault Log")

        if doc.meta.has_field("action"):
            doc.action = action

        if doc.meta.has_field("status"):
            doc.status = status

        if doc.meta.has_field("file") and file_id and frappe.db.exists("File", file_id):
            doc.file = file_id

        if doc.meta.has_field("error_message"):
            doc.error_message = message

        if doc.meta.has_field("message"):
            doc.message = message

        if doc.meta.has_field("details"):
            doc.details = message

        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)

    except Exception:
        pass


@frappe.whitelist()
def download(file):
    from frappe_s3_vault.utils import s3_client

    if not frappe.db.exists("File", file):
        frappe.throw("File record not found or deleted")

    file_doc = frappe.get_doc("File", file)

    vault_name = frappe.db.get_value(
        "S3 Vault File",
        {
            "file": file,
            "status": "Uploaded",
        },
        "name",
        order_by="creation desc",
    )

    if not vault_name:
        frappe.throw("This file was deleted or is not available")

    vault = frappe.get_doc("S3 Vault File", vault_name)

    if not vault.object_key:
        frappe.throw("This file has no storage object key")

    bucket_doc = frappe.get_doc("S3 Vault Bucket", vault.bucket)
    client = s3_client(bucket_doc)

    try:
        client.head_object(
            Bucket=bucket_doc.bucket_name,
            Key=vault.object_key,
        )
    except Exception:
        try:
            frappe.db.set_value(
                "S3 Vault File",
                vault.name,
                "status",
                "Missing",
                update_modified=False,
            )
            frappe.db.commit()
        except Exception:
            pass

        safe_log("Download", "Failed", file, "Wasabi object key missing")
        frappe.throw("This file was deleted from storage and is no longer available")

    url = client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket_doc.bucket_name,
            "Key": vault.object_key,
        },
        ExpiresIn=300,
    )

    safe_log("Download", "Success", file, vault.object_key)

    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = url


@frappe.whitelist()
def test_connection(bucket):
    from frappe_s3_vault.utils import s3_client

    bucket_doc = frappe.get_doc("S3 Vault Bucket", bucket)
    client = s3_client(bucket_doc)

    client.head_bucket(Bucket=bucket_doc.bucket_name)

    return "Connection successful"
