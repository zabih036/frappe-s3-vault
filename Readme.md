# Frappe S3 Vault

`frappe_s3_vault` is a Frappe/ERPNext app for storing Frappe files and Raven chat attachments in S3-compatible object storage such as **Wasabi**, while keeping Frappe `File` records, audit logs, download access, and delete behavior controlled from inside Frappe.

The app is designed for organizations that want to reduce local server storage usage, keep attachments centralized in object storage, and still manage access through Frappe permissions and configurable rules.

---

## Table of Contents

- [Overview](#overview)
- [Main Features](#main-features)
- [Supported Use Cases](#supported-use-cases)
- [App Architecture](#app-architecture)
- [Main DocTypes](#main-doctypes)
- [S3 Vault Rule Options](#s3-vault-rule-options)
- [How File Upload Works](#how-file-upload-works)
- [How Raven Upload Works](#how-raven-upload-works)
- [How Download Works](#how-download-works)
- [How Delete Works](#how-delete-works)
- [Installation](#installation)
- [Post-Installation Setup](#post-installation-setup)
- [Wasabi/S3 Bucket Configuration](#wasabis3-bucket-configuration)
- [Creating S3 Vault Rules](#creating-s3-vault-rules)
- [Recommended Raven Rule](#recommended-raven-rule)
- [Recommended Sales Order Rule](#recommended-sales-order-rule)
- [Production Deployment](#production-deployment)
- [Testing Checklist](#testing-checklist)
- [Useful SQL Checks](#useful-sql-checks)
- [Troubleshooting](#troubleshooting)
- [Security Notes](#security-notes)
- [Maintenance](#maintenance)

---

## Overview

Frappe normally stores uploaded files under the site folder:

```text
sites/<site-name>/public/files/
sites/<site-name>/private/files/
```

For large systems, especially when Raven chat is used heavily, this can increase server disk usage quickly.

`frappe_s3_vault` solves this by uploading matched files to an S3-compatible bucket and replacing the local file URL with a secure API URL:

```text
/api/method/frappe_s3_vault.api.download?file=<FILE_ID>
```

The app keeps a storage record in `S3 Vault File` and audit records in `S3 Vault Log`.

---

## Main Features

- Upload Frappe attachments to S3-compatible storage.
- Support Wasabi and other S3-compatible providers.
- Support Raven chat files and image messages.
- Replace Raven file paths before message save.
- Keep Frappe `File` records linked to the original document.
- Delete local files after successful upload.
- Download files through a secure Frappe API endpoint.
- Generate presigned S3 download URLs.
- Create upload, download, and delete audit logs.
- Delete objects from S3 when the rule allows it.
- Preserve objects in S3 when the rule does not allow delete.
- Support allowed and blocked file extensions.
- Support maximum file size rules.
- Support per-DocType upload rules.
- Support dynamic Attach field creation from rules.
- Support normal Frappe document attachments such as Sales Order.
- Support Raven chat attachments with custom pre-save handling.

---

## Supported Use Cases

This app can be used for:

- Raven chat attachments.
- ERPNext document attachments.
- Sales Order attachments.
- Purchase Order attachments.
- Employee or HR document attachments.
- Custom DocType attachments.
- Private file storage.
- Centralized attachment storage in Wasabi/S3.
- Reducing local disk usage on Frappe servers.

---

## App Architecture

The app works around four main layers:

```text
Frappe File
    ↓
S3 Vault Rule
    ↓
S3 / Wasabi Upload
    ↓
S3 Vault File + S3 Vault Log
```

For normal Frappe attachments:

```text
File uploaded
→ File hook runs
→ matching enabled S3 Vault Rule is selected
→ file is uploaded to S3
→ File.file_url becomes secure API URL
→ local file is deleted if rule allows
```

For Raven:

```text
Raven file uploaded
→ Raven Message validate/before_insert hook runs
→ Raven file is uploaded/prepared before message save
→ Raven Message.file and Raven Message.content are changed to secure API URL
→ receiver sees the correct S3 Vault URL from the beginning
```

---

## Main DocTypes

### 1. S3 Vault Settings

Global settings for the app.

Typical fields:

- `enabled`
- `default_bucket`
- `default_url_expiry_seconds`
- `keep_local_copy_days_default`
- `global_blocked_extensions`

Use this DocType to enable or disable the app globally and set fallback behavior.

---

### 2. S3 Vault Bucket

Stores connection details for an S3-compatible bucket.

Typical fields:

- `bucket_title`
- `bucket_name`
- `endpoint_url`
- `region`
- `access_key`
- `secret_key`
- `use_ssl`
- `verify_ssl`
- `signature_version`
- `addressing_style`
- `base_prefix`
- `storage_class`
- `server_side_encryption`
- `kms_key_id`
- `is_active`

For Wasabi, the endpoint usually looks like:

```text
https://s3.<region>.wasabisys.com
```

Example:

```text
https://s3.eu-central-1.wasabisys.com
```

---

### 3. S3 Vault Rule

Controls which files are uploaded, how they are named, which bucket is used, what extensions are allowed, and what happens on delete.

This is the most important configuration DocType.

---

### 4. S3 Vault File

Stores one storage record per uploaded file.

Important fields:

- `file`
- `attached_to_doctype`
- `attached_to_name`
- `bucket`
- `bucket_name`
- `object_key`
- `rule_used`
- `status`
- `file_hash`
- `etag`
- `version_id`
- `local_file_url`
- `file_url`
- `local_file_deleted`
- `deleted_from_storage`
- `deleted_on`
- `deleted_by`

Common statuses:

```text
Pending
Uploaded
Failed
Soft Deleted
Deleted
Missing
```

---

### 5. S3 Vault Log

Audit log for storage actions.

Common actions:

```text
Upload
Download
Delete
Health Check
```

Common statuses:

```text
Success
Failed
```

This DocType helps track what happened to each file.

---

## S3 Vault Rule Options

| Field | Purpose |
|---|---|
| `enabled` | If unchecked, the rule must not upload files. |
| `priority` | Controls which rule is selected first when multiple rules match. |
| `reference_doctype` | The DocType this rule applies to, for example `Raven Message` or `Sales Order`. |
| `bucket` | The S3 Vault Bucket used for upload. |
| `applies_to` | Controls whether the rule applies to all attachments, file manager attachments, or a specific Attach field. |
| `attach_fieldname` | Used when `applies_to` is `Specific Attach Field`. |
| `folder_pattern` | Controls the S3 object folder path. |
| `filename_strategy` | Controls how the stored filename is generated. |
| `is_private` | Uploads object as private when enabled. |
| `require_frappe_permission_check` | Requires Frappe read permission before download. |
| `generate_presigned_url` | Generates a presigned S3 URL for download. |
| `url_expiry_seconds` | Presigned URL expiry time. |
| `max_file_size_mb` | Blocks files larger than this size. |
| `allowed_extensions` | Allows only these file extensions. |
| `blocked_extensions` | Blocks these file extensions. |
| `allowed_mime_types` | Optional MIME type allowlist. |
| `allow_download` | If unchecked, download should be blocked. |
| `allow_preview` | Controls preview behavior where supported. |
| `allow_delete_from_s3` | If checked, delete object from S3 when Frappe file is deleted. |
| `soft_delete_days` | Used for soft-delete retention logic. |
| `delete_local_after_upload` | Deletes local file after successful S3 upload. |
| `keep_local_copy_days` | Keeps local copy for a number of days when supported. |
| `background_upload` | Controls whether upload can run in background for normal Frappe files. |
| `enable_versioning` | Stores S3 version information when supported. |
| `create_attach_field` | Creates a custom Attach field in the target DocType. |
| `dynamic_field_label` | Label for the created Attach field. |
| `dynamic_fieldname` | Fieldname for the created Attach field. |
| `dynamic_fieldtype` | Usually `Attach` or `Attach Image`. |
| `insert_after` | Where to insert the dynamic field. |
| `custom_field_created` | Stores the created Custom Field reference. |

---

## How File Upload Works

For normal Frappe attachments:

1. User uploads a file to a document.
2. Frappe creates a `File` record.
3. `frappe_s3_vault` checks if there is an enabled matching `S3 Vault Rule`.
4. The file extension and size are validated.
5. The file is uploaded to the selected S3 bucket.
6. `S3 Vault File` is created or updated.
7. `S3 Vault Log` records the upload.
8. The original `File.file_url` is replaced with:

```text
/api/method/frappe_s3_vault.api.download?file=<FILE_ID>
```

9. The local file is deleted if:

```text
delete_local_after_upload = 1
keep_local_copy_days = 0
```

---

## How Raven Upload Works

Raven needs special handling because messages are sent immediately and the sender/receiver should not see the temporary local file URL.

The app uses Raven Message hooks to prepare file URLs before the message is saved.

Expected behavior:

```text
Raven local file URL
→ uploaded to S3
→ File.file_url changed to secure API URL
→ Raven Message.file changed to secure API URL
→ Raven Message.content changed to secure API URL
```

This prevents Raven messages from showing broken local paths such as:

```text
/private/files/example.png
```

---

## How Download Works

Files are downloaded through:

```text
/api/method/frappe_s3_vault.api.download?file=<FILE_ID>
```

The endpoint should:

1. Find the Frappe `File`.
2. Find the related `S3 Vault File`.
3. Check that the storage record is uploaded.
4. Check rule permissions when enabled.
5. Check whether object exists in S3.
6. Generate a presigned download URL.
7. Create a `Download` log.
8. Redirect the user to the presigned S3 URL.

---

## How Delete Works

### Normal Frappe Files

When a user deletes a normal document attachment:

1. File delete hook runs.
2. Related `S3 Vault File` record is found.
3. If `allow_delete_from_s3 = 1`, the object is deleted from S3.
4. If `allow_delete_from_s3 = 0`, the object is preserved.
5. `S3 Vault File.file` is cleared to prevent linked document blocking.
6. `S3 Vault File.status` becomes `Deleted` or `Soft Deleted`.
7. `S3 Vault Log.file` is cleared.
8. A new `Delete` log is created.

### Raven Files

Raven delete uses special cleanup because Raven messages and file cards need to be cleaned correctly.

Expected Raven delete behavior:

```text
Delete Raven message/file
→ delete object from S3 if allowed
→ clear File link
→ mark S3 Vault File as Deleted
→ create Delete log
→ Raven no longer shows the file card
```

---

## Installation

### Requirements

- Frappe Framework v16.
- ERPNext v16, if used with ERPNext documents.
- Python environment managed by bench.
- Working MariaDB.
- Working Redis and Supervisor.
- S3-compatible bucket such as Wasabi.
- Raven app, if Raven chat attachment support is needed.

### Install from Git Repository

Go to your bench directory:

```bash
cd ~/frappe-bench
```

Get the app:

```bash
bench get-app frappe_s3_vault https://github.com/zabih036/frappe-s3-vault.git
```

Install on your site:

```bash
bench --site aogc_v16 install-app frappe_s3_vault
```

Run migration:

```bash
bench --site aogc_v16 migrate
```

Clear cache and restart:

```bash
bench --site aogc_v16 clear-cache
bench restart
```

---

## Post-Installation Setup

After installation:

1. Open Desk.
2. Search for **S3 Vault Settings**.
3. Enable the app.
4. Create an **S3 Vault Bucket**.
5. Test bucket connection.
6. Create one or more **S3 Vault Rules**.
7. Upload a test file.
8. Check `S3 Vault File`.
9. Check `S3 Vault Log`.

---

## Wasabi/S3 Bucket Configuration

Create a new `S3 Vault Bucket`.

Example Wasabi settings:

| Field | Example |
|---|---|
| Bucket Title | `Raven Attachments` |
| Bucket Name | `erp-attachments` |
| Endpoint URL | `https://s3.eu-central-1.wasabisys.com` |
| Region | `eu-central-1` |
| Use SSL | `1` |
| Verify SSL | `1` |
| Signature Version | `s3v4` |
| Addressing Style | `auto` |
| Base Prefix | optional |
| Is Active | `1` |

Then enter:

```text
Access Key
Secret Key
```

Use **Test Connection** if available.

---

## Creating S3 Vault Rules

Create a new `S3 Vault Rule`.

Required fields:

```text
enabled
reference_doctype
bucket
folder_pattern
filename_strategy
allowed_extensions
blocked_extensions
allow_download
allow_delete_from_s3
delete_local_after_upload
```

Recommended folder pattern:

```text
{site}/{doctype}/{docname}/{yyyy}/{mm}
```

This creates organized object keys such as:

```text
aogc_v16/Raven_Message/abc123/2026/06/file.png
```

---

## Recommended Raven Rule

For Raven chat attachments:

| Field | Recommended Value |
|---|---|
| Enabled | `1` |
| Priority | `1` |
| Reference DocType | `Raven Message` |
| Bucket | Your Raven/attachments bucket |
| Applies To | `All Attachments` |
| Folder Pattern | `{site}/{doctype}/{docname}/{yyyy}/{mm}` |
| Filename Strategy | `Hash Prefix` |
| Is Private | `1` |
| Require Frappe Permission Check | `1` |
| Generate Presigned URL | `1` |
| URL Expiry Seconds | `900` |
| Max File Size MB | `20` |
| Allowed Extensions | `pdf,jpg,png,docx,xlsx` |
| Blocked Extensions | `exe,bat,sh,php,js,html` |
| Allow Download | `1` |
| Allow Preview | `1` |
| Allow Delete From S3 | `1` |
| Delete Local After Upload | `1` |
| Keep Local Copy Days | `0` |
| Background Upload | `1` |
| Enable Versioning | optional |

---

## Recommended Sales Order Rule

For Sales Order attachments:

| Field | Recommended Value |
|---|---|
| Enabled | `1` |
| Priority | `1` |
| Reference DocType | `Sales Order` |
| Bucket | Your ERP attachments bucket |
| Applies To | `All Attachments` |
| Folder Pattern | `{site}/{doctype}/{docname}/{yyyy}/{mm}` |
| Filename Strategy | `Hash Prefix` |
| Is Private | `1` |
| Require Frappe Permission Check | `1` |
| Generate Presigned URL | `1` |
| URL Expiry Seconds | `900` |
| Allowed Extensions | `pdf,jpg,png,docx,xlsx` |
| Blocked Extensions | `exe,bat,sh,php,js,html` |
| Allow Download | `1` |
| Allow Delete From S3 | `1` |
| Delete Local After Upload | `1` |
| Keep Local Copy Days | `0` |

---

## Production Deployment

On the production server:

```bash
cd ~/frappe-bench/apps/frappe_s3_vault
git pull origin main
```

Then:

```bash
cd ~/frappe-bench
bench --site aogc_v16 migrate
bench --site aogc_v16 clear-cache
bench restart
```

If `bench restart` asks for sudo, enter the correct server password or ask the server administrator.

---

## Testing Checklist

### Raven Upload Test

1. Upload a `.jpg` or `.png` in Raven.
2. Confirm the file appears immediately for sender and receiver.
3. Refresh the page.
4. Confirm the file still opens.
5. Check `S3 Vault File`.
6. Check `S3 Vault Log`.

Expected:

```text
S3 Vault File.status = Uploaded
File.file_url = /api/method/frappe_s3_vault.api.download?file=<file_id>
Upload log = Success
Download log = Success after opening file
```

### Raven Disabled Rule Test

1. Set Raven rule `enabled = 0`.
2. Upload a Raven file.
3. It should not upload to S3.
4. Restore `enabled = 1`.

SQL:

```bash
bench --site aogc_v16 mariadb -e "
update `tabS3 Vault Rule`
set enabled=0
where name='raven attachment rule';
"
```

Restore:

```bash
bench --site aogc_v16 mariadb -e "
update `tabS3 Vault Rule`
set enabled=1
where name='raven attachment rule';
"
```

### Allowed Extension Test

Temporarily allow only PNG:

```bash
bench --site aogc_v16 mariadb -e "
update `tabS3 Vault Rule`
set allowed_extensions='png'
where name='raven attachment rule';
"
```

Expected:

```text
.jpg should be blocked
.png should upload
```

Restore:

```bash
bench --site aogc_v16 mariadb -e "
update `tabS3 Vault Rule`
set allowed_extensions='pdf,jpg,png,docx,xlsx'
where name='raven attachment rule';
"
```

### Sales Order Upload/Delete Test

1. Open a Sales Order.
2. Upload an attachment.
3. Confirm `S3 Vault File` record is created.
4. Delete the attachment.
5. Confirm File deletion is not blocked.
6. Confirm S3 object is deleted if rule allows.
7. Confirm `S3 Vault File.status = Deleted`.
8. Confirm `S3 Vault Log` has a Delete log.

---

## Useful SQL Checks

### Latest Storage Records

```bash
bench --site aogc_v16 mariadb -e "
select name,file,attached_to_doctype,attached_to_name,status,rule_used,bucket_name,object_key,local_file_deleted,deleted_from_storage
from `tabS3 Vault File`
order by modified desc
limit 10;
"
```

### Latest Logs

```bash
bench --site aogc_v16 mariadb -e "
select name,action,status,file,storage_file,bucket_name,object_key,error_message
from `tabS3 Vault Log`
order by creation desc
limit 10;
"
```

### Check Raven Rule

```bash
bench --site aogc_v16 mariadb -e "
select name,enabled,allowed_extensions,blocked_extensions,allow_download,allow_preview,allow_delete_from_s3,delete_local_after_upload
from `tabS3 Vault Rule`
where name='raven attachment rule'\G
"
```

### Check File URL

```bash
bench --site aogc_v16 mariadb -e "
select name,file_name,file_url,attached_to_doctype,attached_to_name
from tabFile
order by creation desc
limit 10;
"
```

---

## Troubleshooting

### File uploads to chat but disappears after refresh

Possible causes:

- Extension is blocked.
- No enabled rule matches.
- Raven pre-save hook did not convert URL.
- Upload failed after Raven message was created.

Check:

```bash
bench --site aogc_v16 mariadb -e "
select name,action,status,file,storage_file,error_message
from `tabS3 Vault Log`
order by creation desc
limit 10;
"
```

### Download is blocked

Check:

```bash
bench --site aogc_v16 mariadb -e "
select name,allow_download,allow_preview
from `tabS3 Vault Rule`
where name='raven attachment rule'\G
"
```

If `allow_download = 0`, the download API should block the file.

### File delete is blocked by linked documents

This means `S3 Vault File.file` or `S3 Vault Log.file` still links to the File.

Check:

```bash
bench --site aogc_v16 mariadb -e "
select name,file,status,attached_to_doctype,attached_to_name
from `tabS3 Vault File`
where file='PUT_FILE_ID_HERE';

select name,action,status,file,storage_file
from `tabS3 Vault Log`
where file='PUT_FILE_ID_HERE';
"
```

Expected delete cleanup should clear these links.

### Object key missing

If you see:

```text
Value missing for S3 Vault File: Object Key
```

Check object key generation:

```bash
grep -n "make_object_key\|object_key" apps/frappe_s3_vault/frappe_s3_vault/upload.py
grep -n "def make_object_key" apps/frappe_s3_vault/frappe_s3_vault/utils.py
```

Do not add emergency fallback logic unless the exact upload path is understood.

### Raven Channel owner column error

If you see:

```text
Unknown column 'tabRaven Channel.owner' in 'WHERE'
```

Reload and migrate Raven DocTypes:

```bash
bench --site aogc_v16 reload-doc raven doctype raven_channel
bench --site aogc_v16 migrate
bench --site aogc_v16 clear-cache
bench restart
```

---

## Security Notes

Recommended settings:

```text
is_private = 1
require_frappe_permission_check = 1
generate_presigned_url = 1
url_expiry_seconds = 900
allow_download = 1 only where needed
blocked_extensions = exe,bat,sh,php,js,html
delete_local_after_upload = 1
keep_local_copy_days = 0
```

Avoid allowing executable or script files unless there is a strong business need.

Recommended blocked extensions:

```text
exe,bat,cmd,sh,php,js,html,msi,jar,py,ps1,vbs
```

---

## Maintenance

### Clean Python cache

```bash
cd ~/frappe-bench/apps/frappe_s3_vault

find frappe_s3_vault -type d -name "__pycache__" -prune -exec rm -rf {} +
find frappe_s3_vault -type f -name "*.pyc" -delete
```

### Check Git Status

```bash
cd ~/frappe-bench/apps/frappe_s3_vault

git status --short
```

### Commit Changes

```bash
git add .
git commit -m "Describe your change"
git push origin main
```

### Pull in Production

```bash
cd ~/frappe-bench/apps/frappe_s3_vault

git pull origin main

cd ~/frappe-bench
bench --site aogc_v16 migrate
bench --site aogc_v16 clear-cache
bench restart
```

---

## Recommended Production Rules

For production, use strict rules:

```text
enabled = 1
is_private = 1
require_frappe_permission_check = 1
generate_presigned_url = 1
url_expiry_seconds = 900
max_file_size_mb = 20
allowed_extensions = pdf,jpg,png,docx,xlsx
blocked_extensions = exe,bat,cmd,sh,php,js,html,msi,jar,py,ps1,vbs
allow_download = 1
allow_delete_from_s3 = 1
delete_local_after_upload = 1
keep_local_copy_days = 0
```

---

## Summary

`frappe_s3_vault` provides controlled S3/Wasabi storage for Frappe and Raven attachments.

It helps with:

- Reducing local disk usage.
- Keeping attachment storage centralized.
- Supporting private S3 objects.
- Keeping Frappe permission-based downloads.
- Supporting Raven chat files.
- Logging upload/download/delete actions.
- Respecting per-DocType upload and delete rules.

The most important configuration is the `S3 Vault Rule`. Always test rules carefully before using them in production.
