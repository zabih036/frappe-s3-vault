frappe.ui.form.on("S3 Vault Index Configuration", {
	refresh(frm) {
		if (!frm.is_new() && frm.doc.enabled) {
			frm.add_custom_button(__("Rebuild Index"), async () => {
				const response = await frappe.call({
					method: "frappe_s3_vault.file_manager_index.start_rebuild",
					args: { connection: frm.doc.connection },
					freeze: true,
				});
				if (response.message?.name) {
					frappe.set_route("Form", "S3 Vault Operation", response.message.name);
				}
			}, __("Actions"));
		}
	},
});
