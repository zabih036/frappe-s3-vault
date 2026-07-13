frappe.ui.form.on("S3 Vault Access Rule", {
	principal_type(frm) {
		if (frm.doc.principal_type === "Role") frm.set_value("user", null);
		if (frm.doc.principal_type === "User") frm.set_value("role", null);
	},
});
