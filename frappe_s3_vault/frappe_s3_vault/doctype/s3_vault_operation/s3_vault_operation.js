frappe.ui.form.on("S3 Vault Operation", {
	onload(frm) {
		if (!frappe.realtime || frm.__s3_vault_realtime_bound) return;
		frm.__s3_vault_realtime_bound = true;
		frappe.realtime.on("s3_vault_operation_progress", (operation) => {
			if (operation?.name === frm.doc.name) {
				frm.reload_doc();
			}
		});
	},

	refresh(frm) {
		const status_colors = {
			Queued: "orange",
			Running: "blue",
			Completed: "green",
			"Partially Completed": "orange",
			Failed: "red",
			Cancelled: "gray",
		};
		frm.page.set_indicator(
			__(frm.doc.status || "Unknown"),
			status_colors[frm.doc.status] || "gray"
		);

		if (frm.doc.connection) {
			frm.add_custom_button(__("Open File Manager"), () => {
				frappe.set_route("s3-file-manager");
			});
		}

		if (
			frm.doc.status === "Completed" &&
			frm.doc.result_key &&
			!frm.doc.result_deleted
		) {
			frm.add_custom_button(__("Download Result"), async () => {
				const response = await frappe.call({
					method: "frappe_s3_vault.file_manager.get_operation_result_url",
					args: { operation_name: frm.doc.name },
				});
				if (response.message?.url) {
					const anchor = document.createElement("a");
					anchor.href = response.message.url;
					anchor.rel = "noopener";
					anchor.download = response.message.filename || "";
					document.body.appendChild(anchor);
					anchor.click();
					anchor.remove();
				}
			});
		}
	},
});
