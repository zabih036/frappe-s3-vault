(function () {
    let reloadTimer = null;

    function is_raven_page() {
        try {
            const path = (window.location.pathname || "").toLowerCase();
            const hash = (window.location.hash || "").toLowerCase();

            if (path.includes("raven") || hash.includes("raven")) {
                return true;
            }

            if (window.frappe && frappe.get_route) {
                const route = (frappe.get_route() || []).join("/").toLowerCase();
                if (route.includes("raven")) {
                    return true;
                }
            }
        } catch (e) {}

        return false;
    }

    function reload_raven_once(data) {
        if (!data || data.attached_to_doctype !== "Raven Message") {
            return;
        }

        if (!is_raven_page()) {
            return;
        }

        const key = "s3_vault_raven_reload_" + data.file;
        const now = Date.now();
        const last = Number(sessionStorage.getItem(key) || 0);

        // Avoid reload loop for same file.
        if (now - last < 15000) {
            return;
        }

        sessionStorage.setItem(key, String(now));

        try {
            frappe.show_alert({
                message: __("Attachment moved to secure storage. Refreshing Raven..."),
                indicator: "green"
            }, 3);
        } catch (e) {}

        clearTimeout(reloadTimer);

        reloadTimer = setTimeout(function () {
            window.location.reload();
        }, 1000);
    }

    function refresh_current_form_for_s3_vault(data) {
        if (!data) return;

        // Raven is React-based and does not use normal Desk attachment refresh.
        // For Raven, reload once after upload completes.
        if (data.attached_to_doctype === "Raven Message") {
            reload_raven_once(data);
            return;
        }

        const frm = window.cur_frm;

        if (!frm || !frm.doc) return;

        const same_doc =
            data.attached_to_doctype === frm.doctype &&
            data.attached_to_name === frm.docname;

        if (!same_doc) return;

        try {
            if (frm.attachments && frm.attachments.refresh) {
                frm.attachments.refresh();
            }
        } catch (e) {
            console.warn("S3 Vault attachment refresh failed", e);
        }

        setTimeout(function () {
            try {
                frm.reload_doc();
            } catch (e) {
                console.warn("S3 Vault form reload failed", e);
            }
        }, 700);

        try {
            frappe.show_alert({
                message: __("Attachment moved to secure storage"),
                indicator: "green"
            }, 4);
        } catch (e) {}
    }

    function bind_events() {
        if (!window.frappe || !frappe.realtime) {
            return false;
        }

        frappe.realtime.on("s3_vault_file_uploaded", refresh_current_form_for_s3_vault);
        frappe.realtime.on("s3_vault_raven_message_uploaded", reload_raven_once);
        frappe.realtime.on("s3_vault_raven_message_updated", reload_raven_once);

        return true;
    }

    if (!bind_events()) {
        document.addEventListener("DOMContentLoaded", function () {
            bind_events();
        });
    }
})();
