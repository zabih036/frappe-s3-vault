frappe.ui.form.on("S3 Vault Operation", {
	refresh(frm) {
		if (["Queued", "Running"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Request Cancellation"), async () => {
				await frappe.call({
					method: "frappe_s3_vault.file_manager.cancel_operation",
					args: { operation_name: frm.doc.name },
					freeze: true,
				});
				frm.reload_doc();
			}, __("Actions"));
		}
		if (["Failed", "Partially Completed", "Cancelled"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Retry"), async () => {
				const response = await frappe.call({
					method: "frappe_s3_vault.file_manager.retry_operation",
					args: { operation_name: frm.doc.name },
					freeze: true,
				});
				if (response.message?.name) {
					frappe.set_route("Form", "S3 Vault Operation", response.message.name);
				}
			}, __("Actions"));
		}
	},
});
