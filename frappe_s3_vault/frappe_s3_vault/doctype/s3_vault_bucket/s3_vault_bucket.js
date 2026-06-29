frappe.ui.form.on("S3 Vault Bucket", {
    refresh(frm) {
        frm.add_custom_button("Test Connection", () => {
            frappe.call({
                method: "frappe_s3_vault.api.test_connection",
                args: {
                    bucket: frm.doc.name
                },
                freeze: true,
                freeze_message: "Testing S3 connection...",
                callback(r) {
                    if (!r.exc) {
                        frappe.msgprint(r.message || "Connection successful");
                        frm.reload_doc();
                    }
                }
            });
        });
    }
});
