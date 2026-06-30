frappe.listview_settings["S3 Vault Log"] = {
    add_fields: [
        "action",
        "status",
        "user",
        "storage_file",
        "file",
        "doctype_name",
        "document_name",
        "bucket_name",
        "object_key"
    ],

    get_indicator(doc) {
        if (doc.status === "Success") {
            return [__("Success"), "green", "status,=,Success"];
        }

        if (doc.status === "Failed") {
            return [__("Failed"), "red", "status,=,Failed"];
        }

        return [__(doc.status || "Unknown"), "gray", `status,=,${doc.status}`];
    }
};
