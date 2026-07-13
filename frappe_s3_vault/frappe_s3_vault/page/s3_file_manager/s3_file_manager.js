frappe.pages["s3-file-manager"].on_page_load = function (wrapper) {
	wrapper.s3_file_manager = new S3FileManager(wrapper);
};

frappe.pages["s3-file-manager"].on_page_show = function (wrapper) {
	if (wrapper.s3_file_manager) {
		wrapper.s3_file_manager.on_page_show();
	}
};

class S3FileManager {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.api = "frappe_s3_vault.file_manager";
		this.connection = null;
		this.current_prefix = "";
		this.current_token = null;
		this.previous_tokens = [];
		this.next_token = null;
		this.page_size = 100;
		this.data = { folders: [], files: [] };
		this.connection_rows = [];
		this.selected = new Map();
		this.operations = [];
		this.operations_visible = false;
		this.operation_poll_timer = null;
		this.initialized = false;

		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("S3 File Manager"),
			single_column: true,
		});

		this.make_layout();
		this.bind_events();
		this.bind_realtime();
		this.load_connections();
	}

	on_page_show() {
		if (this.initialized && this.connection) {
			this.load_recent_operations(false);
		}
	}

	make_layout() {
		this.$root = $(`
			<div class="s3fm">
				<div class="s3fm-toolbar">
					<div class="s3fm-connection-wrap">
						<label>${__("S3 Connection")}</label>
						<div class="s3fm-connection-control"></div>
					</div>

					<div class="s3fm-search-wrap">
						<label>${__("Search current page")}</label>
						<input
							type="search"
							class="form-control s3fm-search"
							placeholder="${__("Search folders and files")}"
						>
					</div>

					<div class="s3fm-page-size-wrap">
						<label>${__("Rows")}</label>
						<select class="form-control s3fm-page-size">
							<option value="100">100</option>
							<option value="250">250</option>
							<option value="500">500</option>
							<option value="1000">1000</option>
						</select>
					</div>

					<div class="s3fm-actions">
						<button class="btn btn-default s3fm-operations-toggle">
							${frappe.utils.icon("activity", "sm")}
							${__("Operations")}
							<span class="s3fm-operation-count hidden">0</span>
						</button>
						<button class="btn btn-default s3fm-new-folder">
							${frappe.utils.icon("folder-plus", "sm")}
							${__("New Folder")}
						</button>
						<button class="btn btn-primary s3fm-upload">
							${frappe.utils.icon("upload", "sm")}
							${__("Upload")}
						</button>
						<button class="btn btn-default s3fm-refresh" title="${__("Refresh")}">
							${frappe.utils.icon("refresh", "sm")}
						</button>
						<input type="file" class="s3fm-file-input" multiple hidden>
					</div>
				</div>

				<div class="s3fm-connection-summary"></div>
				<div class="s3fm-breadcrumb"></div>

				<div class="s3fm-bulk-toolbar hidden">
					<div class="s3fm-bulk-summary">
						<strong class="s3fm-selected-count">0</strong>
						<span>${__("selected")}</span>
					</div>
					<div class="s3fm-bulk-actions">
						<button class="btn btn-default btn-sm s3fm-bulk-copy">${__("Copy")}</button>
						<button class="btn btn-default btn-sm s3fm-bulk-move">${__("Move")}</button>
						<button class="btn btn-default btn-sm s3fm-bulk-zip">${__("Download ZIP")}</button>
						<button class="btn btn-danger btn-sm s3fm-bulk-delete">${__("Delete")}</button>
						<button class="btn btn-default btn-sm s3fm-clear-selection">${__("Clear")}</button>
					</div>
				</div>

				<div class="s3fm-upload-status hidden">
					<div class="s3fm-upload-title"></div>
					<div class="progress">
						<div
							class="progress-bar"
							role="progressbar"
							style="width: 0%"
							aria-valuemin="0"
							aria-valuemax="100"
						>0%</div>
					</div>
				</div>

				<div class="s3fm-operations-panel hidden">
					<div class="s3fm-operations-header">
						<div>
							<div class="s3fm-operations-title">${__("Background Operations")}</div>
							<div class="text-muted">${__("Folder, bulk, and ZIP jobs run on the long worker queue.")}</div>
						</div>
						<div class="s3fm-operations-header-actions">
							<button class="btn btn-default btn-sm s3fm-open-all-operations">${__("Open All")}</button>
							<button class="btn btn-default btn-sm s3fm-refresh-operations">${__("Refresh")}</button>
							<button class="btn btn-default btn-sm s3fm-close-operations">${__("Close")}</button>
						</div>
					</div>
					<div class="s3fm-operation-list"></div>
				</div>

				<div class="s3fm-shell">
					<aside class="s3fm-sidebar">
						<div class="s3fm-sidebar-title">${__("Folders")}</div>
						<div class="s3fm-folder-list"></div>
					</aside>

					<section class="s3fm-main">
						<div class="s3fm-loading hidden">
							<div class="spinner-border spinner-border-sm"></div>
							<span>${__("Loading bucket contents...")}</span>
						</div>

						<div class="s3fm-table-wrap">
							<table class="table s3fm-table">
								<thead>
									<tr>
										<th class="s3fm-select-column">
											<input type="checkbox" class="s3fm-select-all" aria-label="${__("Select all visible rows")}">
										</th>
										<th>${__("Name")}</th>
										<th class="s3fm-type-column">${__("Type")}</th>
										<th class="s3fm-size-column">${__("Size")}</th>
										<th class="s3fm-date-column">${__("Modified")}</th>
										<th class="s3fm-action-column">${__("Actions")}</th>
									</tr>
								</thead>
								<tbody></tbody>
							</table>

							<div class="s3fm-empty hidden">
								<div class="s3fm-empty-icon">📂</div>
								<div class="s3fm-empty-title">${__("This folder is empty")}</div>
								<div class="text-muted">${__("Upload a file or create a folder to begin.")}</div>
							</div>
						</div>

						<div class="s3fm-pagination">
							<button class="btn btn-default btn-sm s3fm-previous">${__("Previous")}</button>
							<span class="s3fm-page-info"></span>
							<button class="btn btn-default btn-sm s3fm-next">${__("Next")}</button>
						</div>
					</section>
				</div>

				<div class="s3fm-no-connection hidden">
					<div class="s3fm-empty-icon">☁️</div>
					<h4>${__("No enabled S3 connection found")}</h4>
					<p class="text-muted">${__("Create and enable an S3 Vault Bucket connection first.")}</p>
					<button class="btn btn-primary s3fm-open-connections">${__("Open S3 Vault Buckets")}</button>
				</div>
			</div>
		`).appendTo(this.page.main);

		this.connection_control = frappe.ui.form.make_control({
			parent: this.$root.find(".s3fm-connection-control"),
			df: {
				fieldtype: "Link",
				fieldname: "connection",
				options: "S3 Vault Bucket",
				placeholder: __("Select connection"),
				get_query: () => ({ filters: { enabled: 1 } }),
				change: () => this.on_connection_change(),
			},
			render_input: true,
		});
	}

	bind_events() {
		this.$root.on("click", ".s3fm-refresh", () => this.load_current_folder());
		this.$root.on("click", ".s3fm-new-folder", () => this.show_new_folder_dialog());
		this.$root.on("click", ".s3fm-upload", () => this.$root.find(".s3fm-file-input").trigger("click"));
		this.$root.on("change", ".s3fm-file-input", (event) => this.upload_files(event.target.files));
		this.$root.on("input", ".s3fm-search", () => this.render_items());
		this.$root.on("change", ".s3fm-page-size", (event) => {
			this.page_size = Number(event.target.value) || 100;
			this.reset_pagination();
			this.clear_selection();
			this.load_current_folder();
		});
		this.$root.on("click", ".s3fm-previous", () => this.go_previous_page());
		this.$root.on("click", ".s3fm-next", () => this.go_next_page());
		this.$root.on("click", ".s3fm-open-connections", () => frappe.set_route("List", "S3 Vault Bucket"));
		this.$root.on("click", ".s3fm-open-all-operations", () => frappe.set_route("List", "S3 Vault Operation"));
		this.$root.on("click", ".s3fm-refresh-operations", () => this.load_recent_operations(true));
		this.$root.on("click", ".s3fm-close-operations", () => this.toggle_operations(false));
		this.$root.on("click", ".s3fm-operations-toggle", () => this.toggle_operations(!this.operations_visible));

		this.$root.on("click", "[data-folder-key]", (event) => {
			if ($(event.target).closest("button, input, a").length && !$(event.currentTarget).is("button")) return;
			const key = decodeURIComponent($(event.currentTarget).attr("data-folder-key"));
			this.open_folder(key);
		});
		this.$root.on("click", "[data-breadcrumb-prefix]", (event) => {
			const prefix = decodeURIComponent($(event.currentTarget).attr("data-breadcrumb-prefix"));
			this.open_folder(prefix);
		});
		this.$root.on("click", "[data-preview-key]", (event) => {
			event.stopPropagation();
			this.open_object(decodeURIComponent($(event.currentTarget).attr("data-preview-key")), "inline");
		});
		this.$root.on("click", "[data-download-key]", (event) => {
			event.stopPropagation();
			this.open_object(decodeURIComponent($(event.currentTarget).attr("data-download-key")), "attachment");
		});
		this.$root.on("click", "[data-item-actions]", (event) => {
			event.stopPropagation();
			this.show_item_actions(this.decode_item($(event.currentTarget).attr("data-item-actions")));
		});

		this.$root.on("change", ".s3fm-row-select", (event) => {
			event.stopPropagation();
			const item = this.decode_item($(event.currentTarget).attr("data-item"));
			this.set_selected(item, event.currentTarget.checked);
		});
		this.$root.on("change", ".s3fm-select-all", (event) => this.select_all_visible(event.currentTarget.checked));
		this.$root.on("click", ".s3fm-clear-selection", () => this.clear_selection());
		this.$root.on("click", ".s3fm-bulk-copy", () => this.start_bulk_transfer("copy"));
		this.$root.on("click", ".s3fm-bulk-move", () => this.start_bulk_transfer("move"));
		this.$root.on("click", ".s3fm-bulk-delete", () => this.start_bulk_delete());
		this.$root.on("click", ".s3fm-bulk-zip", () => this.start_bulk_zip());

		this.$root.on("click", "[data-operation-open]", (event) => {
			frappe.set_route("Form", "S3 Vault Operation", $(event.currentTarget).attr("data-operation-open"));
		});
		this.$root.on("click", "[data-operation-download]", (event) => {
			this.download_operation_result($(event.currentTarget).attr("data-operation-download"));
		});
	}

	bind_realtime() {
		if (!frappe.realtime) return;
		frappe.realtime.on("s3_vault_operation_progress", (operation) => {
			if (!operation || (this.connection && operation.connection !== this.connection)) return;
			const index = this.operations.findIndex((row) => row.name === operation.name);
			if (index >= 0) this.operations[index] = operation;
			else this.operations.unshift(operation);
			this.render_operations();
			this.update_operation_badge();
			if (["Completed", "Partially Completed", "Failed"].includes(operation.status)) {
				this.load_current_folder();
			}
		});
	}

	async call(method, args = {}, freeze = false) {
		const response = await frappe.call({
			method: `${this.api}.${method}`,
			args,
			freeze,
			freeze_message: freeze ? __("Processing S3 request...") : undefined,
		});
		return response.message || {};
	}

	async load_connections() {
		try {
			const result = await this.call("get_connections");
			this.connection_rows = result.connections || [];
			if (!this.connection_rows.length) {
				this.show_no_connection();
				return;
			}
			this.hide_no_connection();
			const default_connection = this.connection_control.get_value() || result.default_connection || this.connection_rows[0].name;
			await this.connection_control.set_value(default_connection);
			this.connection = default_connection;
			const selected_connection = this.connection_rows.find((row) => row.name === default_connection) || {};
			this.access_roots = selected_connection.access_roots?.length ? selected_connection.access_roots : [""];
			this.active_root = selected_connection.default_root ?? this.access_roots[0] ?? "";
			this.current_prefix = this.active_root;
			this.render_connection_summary();
			await Promise.all([this.load_current_folder(), this.load_recent_operations(false)]);
			this.initialized = true;
		} catch (error) {
			this.handle_error(error);
		}
	}

	async on_connection_change() {
		const value = this.connection_control.get_value();
		if (!value || value === this.connection) return;
		this.connection = value;
		const selected_connection = this.connection_rows.find((row) => row.name === value) || {};
		this.access_roots = selected_connection.access_roots?.length ? selected_connection.access_roots : [""];
		this.active_root = selected_connection.default_root ?? this.access_roots[0] ?? "";
		this.current_prefix = this.active_root;
		this.reset_pagination();
		this.clear_selection();
		this.$root.find(".s3fm-search").val("");
		this.render_connection_summary();
		await Promise.all([this.load_current_folder(), this.load_recent_operations(false)]);
	}

	render_connection_summary() {
		const row = this.connection_rows.find((item) => item.name === this.connection);
		if (!row) {
			this.$root.find(".s3fm-connection-summary").empty();
			return;
		}
		const prefix = row.base_prefix ? this.escape(row.base_prefix) : "/";
		this.$root.find(".s3fm-connection-summary").html(`
			<span><strong>${__("Bucket")}:</strong> ${this.escape(row.bucket_name)}</span>
			<span><strong>${__("Provider")}:</strong> ${this.escape(row.provider_type || "")}</span>
			<span><strong>${__("Region")}:</strong> ${this.escape(row.region || "")}</span>
			<span><strong>${__("Virtual root")}:</strong> ${prefix}</span>
		`);
	}

	show_no_connection() {
		this.$root.find(".s3fm-toolbar, .s3fm-connection-summary, .s3fm-breadcrumb, .s3fm-shell").addClass("hidden");
		this.$root.find(".s3fm-no-connection").removeClass("hidden");
	}

	hide_no_connection() {
		this.$root.find(".s3fm-toolbar, .s3fm-connection-summary, .s3fm-breadcrumb, .s3fm-shell").removeClass("hidden");
		this.$root.find(".s3fm-no-connection").addClass("hidden");
	}

	reset_pagination() {
		this.current_token = null;
		this.previous_tokens = [];
		this.next_token = null;
	}

	async load_current_folder() {
		if (!this.connection) return;
		this.set_loading(true);
		try {
			const result = await this.call("list_objects", {
				connection: this.connection,
				prefix: this.current_prefix,
				continuation_token: this.current_token,
				page_size: this.page_size,
			});
			this.data = result;
			this.next_token = result.next_token || null;
			this.current_prefix = result.prefix || "";
			this.render_breadcrumb();
			this.render_sidebar();
			this.render_items();
			this.render_pagination();
		} catch (error) {
			this.handle_error(error);
		} finally {
			this.set_loading(false);
		}
	}

	open_folder(prefix) {
		this.current_prefix = prefix || "";
		this.reset_pagination();
		this.clear_selection();
		this.$root.find(".s3fm-search").val("");
		this.load_current_folder();
	}

	go_next_page() {
		if (!this.next_token) return;
		this.previous_tokens.push(this.current_token);
		this.current_token = this.next_token;
		this.clear_selection();
		this.load_current_folder();
	}

	go_previous_page() {
		if (!this.previous_tokens.length) return;
		this.current_token = this.previous_tokens.pop() || null;
		this.clear_selection();
		this.load_current_folder();
	}

	render_breadcrumb() {
		const parts = (this.current_prefix || "").replace(/\/$/, "").split("/").filter(Boolean);
		let accumulated = "";
		const crumbs = [`<button class="s3fm-crumb" data-breadcrumb-prefix="">${frappe.utils.icon("home", "sm")} ${__("Root")}</button>`];
		for (const part of parts) {
			accumulated += `${part}/`;
			crumbs.push(`<span class="s3fm-crumb-separator">/</span>`);
			crumbs.push(`<button class="s3fm-crumb" data-breadcrumb-prefix="${encodeURIComponent(accumulated)}">${this.escape(part)}</button>`);
		}
		this.$root.find(".s3fm-breadcrumb").html(crumbs.join(""));
	}

	render_sidebar() {
		const folders = this.data.folders || [];
		const $list = this.$root.find(".s3fm-folder-list");
		if (!folders.length) {
			$list.html(`<div class="s3fm-sidebar-empty text-muted">${__("No subfolders")}</div>`);
			return;
		}
		$list.html(folders.map((folder) => `
			<button class="s3fm-folder-link" data-folder-key="${encodeURIComponent(folder.key)}" title="${this.escape(folder.name)}">
				<span class="s3fm-folder-icon">📁</span><span>${this.escape(folder.name)}</span>
			</button>
		`).join(""));
	}

	filtered_items() {
		const query = String(this.$root.find(".s3fm-search").val() || "").trim().toLowerCase();
		const items = [...(this.data.folders || []), ...(this.data.files || [])];
		if (!query) return items;
		return items.filter((item) => [item.name, item.key, item.content_type, item.linked?.attached_to_doctype, item.linked?.attached_to_name]
			.some((value) => String(value || "").toLowerCase().includes(query)));
	}

	render_items() {
		const items = this.filtered_items();
		const $tbody = this.$root.find(".s3fm-table tbody");
		const $empty = this.$root.find(".s3fm-empty");
		if (!items.length) {
			$tbody.empty();
			$empty.removeClass("hidden");
			this.sync_select_all_state();
			return;
		}
		$empty.addClass("hidden");
		$tbody.html(items.map((item) => this.render_row(item)).join(""));
		this.sync_select_all_state();
	}

	render_row(item) {
		const selected = this.selected.has(this.item_id(item)) ? "checked" : "";
		const encoded_item = this.encode_item(item);
		if (item.type === "folder") {
			return `
				<tr class="s3fm-row s3fm-folder-row ${selected ? "s3fm-row-selected" : ""}" data-folder-key="${encodeURIComponent(item.key)}">
					<td class="s3fm-select-column"><input type="checkbox" class="s3fm-row-select" data-item="${encoded_item}" ${selected}></td>
					<td><div class="s3fm-name-cell"><span class="s3fm-item-icon">📁</span><div><div class="s3fm-item-name">${this.escape(item.name)}</div><div class="s3fm-item-key">${this.escape(item.key)}</div></div></div></td>
					<td class="s3fm-type-column">${__("Folder")}</td>
					<td class="s3fm-size-column">—</td>
					<td class="s3fm-date-column">—</td>
					<td class="s3fm-action-column"><div class="s3fm-row-actions"><button class="btn btn-default btn-xs" data-folder-key="${encodeURIComponent(item.key)}">${__("Open")}</button><button class="btn btn-default btn-xs" data-item-actions="${encoded_item}">${__("Actions")}</button></div></td>
				</tr>`;
		}
		const preview = this.is_previewable(item.content_type, item.name)
			? `<button class="btn btn-default btn-xs" data-preview-key="${encodeURIComponent(item.key)}">${__("Preview")}</button>` : "";
		const managed = item.linked ? `<span class="s3fm-managed-badge" title="${__("Managed Frappe attachment")}">${__("Managed")}</span>` : "";
		return `
			<tr class="s3fm-row s3fm-file-row ${selected ? "s3fm-row-selected" : ""}">
				<td class="s3fm-select-column"><input type="checkbox" class="s3fm-row-select" data-item="${encoded_item}" ${selected}></td>
				<td><div class="s3fm-name-cell"><span class="s3fm-item-icon">${this.file_icon(item.content_type, item.name)}</span><div><div class="s3fm-item-name">${this.escape(item.name)} ${managed}</div><div class="s3fm-item-key">${this.escape(item.key)}</div></div></div></td>
				<td class="s3fm-type-column">${this.escape(item.content_type || __("File"))}</td>
				<td class="s3fm-size-column">${this.format_bytes(item.size)}</td>
				<td class="s3fm-date-column">${this.format_datetime(item.last_modified)}</td>
				<td class="s3fm-action-column"><div class="s3fm-row-actions">${preview}<button class="btn btn-default btn-xs" data-download-key="${encodeURIComponent(item.key)}">${__("Download")}</button><button class="btn btn-default btn-xs" data-item-actions="${encoded_item}">${__("Actions")}</button></div></td>
			</tr>`;
	}

	render_pagination() {
		this.$root.find(".s3fm-page-info").text(__("Page {0}", [this.previous_tokens.length + 1]));
		this.$root.find(".s3fm-previous").prop("disabled", !this.previous_tokens.length);
		this.$root.find(".s3fm-next").prop("disabled", !this.next_token);
	}

	item_id(item) {
		return `${item.type}:${item.key}`;
	}

	encode_item(item) {
		return encodeURIComponent(JSON.stringify(item));
	}

	decode_item(value) {
		try { return JSON.parse(decodeURIComponent(value)); } catch (error) { return null; }
	}

	set_selected(item, selected) {
		if (!item) return;
		if (selected) this.selected.set(this.item_id(item), item);
		else this.selected.delete(this.item_id(item));
		this.render_bulk_toolbar();
		this.sync_select_all_state();
	}

	select_all_visible(selected) {
		for (const item of this.filtered_items()) {
			if (selected) this.selected.set(this.item_id(item), item);
			else this.selected.delete(this.item_id(item));
		}
		this.render_items();
		this.render_bulk_toolbar();
	}

	clear_selection() {
		this.selected.clear();
		this.$root.find(".s3fm-select-all").prop("checked", false).prop("indeterminate", false);
		this.$root.find(".s3fm-row-select").prop("checked", false);
		this.render_bulk_toolbar();
	}

	sync_select_all_state() {
		const visible = this.filtered_items();
		const selected_visible = visible.filter((item) => this.selected.has(this.item_id(item))).length;
		const $select = this.$root.find(".s3fm-select-all");
		$select.prop("checked", visible.length > 0 && selected_visible === visible.length);
		$select.prop("indeterminate", selected_visible > 0 && selected_visible < visible.length);
	}

	render_bulk_toolbar() {
		const count = this.selected.size;
		this.$root.find(".s3fm-selected-count").text(count);
		this.$root.find(".s3fm-bulk-toolbar").toggleClass("hidden", count === 0);
	}

	async show_new_folder_dialog() {
		if (!this.connection) return;
		const dialog = new frappe.ui.Dialog({
			title: __("Create Folder"),
			fields: [
				{ fieldname: "location", fieldtype: "Data", label: __("Current Location"), read_only: 1, default: this.current_prefix || "/" },
				{ fieldname: "folder_name", fieldtype: "Data", label: __("Folder Name"), reqd: 1 },
			],
			primary_action_label: __("Create"),
			primary_action: async (values) => {
				dialog.disable_primary_action();
				try {
					await this.call("create_folder", { connection: this.connection, prefix: this.current_prefix, folder_name: values.folder_name });
					dialog.hide();
					frappe.show_alert({ message: __("Folder created"), indicator: "green" });
					await this.load_current_folder();
				} catch (error) { dialog.enable_primary_action(); this.handle_error(error); }
			},
		});
		dialog.show();
	}

	async upload_files(file_list) {
		const files = Array.from(file_list || []);
		this.$root.find(".s3fm-file-input").val("");
		if (!files.length || !this.connection) return;
		try {
			for (let index = 0; index < files.length; index++) {
				const file = files[index];
				this.show_upload_progress(0, __("Preparing {0} ({1} of {2})", [file.name, index + 1, files.length]));
				let session;
				try {
					session = await this.call("create_upload_session", {
						connection: this.connection, prefix: this.current_prefix, filename: file.name,
						content_type: file.type || "application/octet-stream", file_size: file.size, overwrite: 0,
					});
				} catch (error) {
					if (!this.is_duplicate_error(error)) throw error;
					const replace = await this.confirm_promise(__("A file named {0} already exists. Replace it?", [file.name]));
					if (!replace) throw error;
					session = await this.call("create_upload_session", {
						connection: this.connection, prefix: this.current_prefix, filename: file.name,
						content_type: file.type || "application/octet-stream", file_size: file.size, overwrite: 1,
					});
				}
				await this.put_file(file, session, index + 1, files.length);
				await this.call("complete_upload", { connection: this.connection, key: session.key, expected_size: file.size });
			}
			this.hide_upload_progress();
			frappe.show_alert({ message: __("Upload completed"), indicator: "green" });
			await this.load_current_folder();
		} catch (error) { this.hide_upload_progress(); this.handle_error(error); }
	}

	put_file(file, session, file_number, total_files) {
		return new Promise((resolve, reject) => {
			const xhr = new XMLHttpRequest();
			xhr.open(session.method || "PUT", session.upload_url, true);
			Object.entries(session.headers || {}).forEach(([name, value]) => xhr.setRequestHeader(name, value));
			xhr.upload.onprogress = (event) => {
				if (!event.lengthComputable) return;
				this.show_upload_progress(Math.round((event.loaded / event.total) * 100), __("Uploading {0} ({1} of {2})", [file.name, file_number, total_files]));
			};
			xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(__("S3 upload failed with HTTP status {0}. Check bucket CORS settings.", [xhr.status])));
			xhr.onerror = () => reject(new Error(__("The browser could not upload to S3. Check CORS, endpoint, and network access.")));
			xhr.send(file);
		});
	}

	show_upload_progress(percent, title) {
		const value = Math.max(0, Math.min(Number(percent) || 0, 100));
		const $status = this.$root.find(".s3fm-upload-status").removeClass("hidden");
		$status.find(".s3fm-upload-title").text(title || __("Uploading..."));
		$status.find(".progress-bar").css("width", `${value}%`).attr("aria-valuenow", value).text(`${value}%`);
	}

	hide_upload_progress() { this.$root.find(".s3fm-upload-status").addClass("hidden"); }

	async open_object(key, disposition) {
		let preview_window = disposition === "inline" ? window.open("about:blank", "_blank") : null;
		try {
			const result = await this.call("get_object_url", { connection: this.connection, key, disposition });
			if (disposition === "inline") {
				if (preview_window) preview_window.location.replace(result.url);
				else window.open(result.url, "_blank", "noopener");
				return;
			}
			this.trigger_download(result.url, result.name || "");
		} catch (error) {
			if (preview_window) preview_window.close();
			this.handle_error(error);
		}
	}

	show_item_actions(item) {
		if (!item) return;
		const actions = item.type === "folder"
			? [
				[__("Open"), () => this.open_folder(item.key)],
				[__("Properties / Size"), () => this.show_properties(item)],
				[__("Rename"), () => this.show_rename_dialog(item)],
				[__("Copy"), () => this.show_transfer_dialog(item, "copy")],
				[__("Move"), () => this.show_transfer_dialog(item, "move")],
				[__("Download ZIP"), () => this.start_folder_zip(item)],
				[__("Delete"), () => this.show_delete_dialog(item), "danger"],
			]
			: [
				[__("Preview"), () => this.open_object(item.key, "inline")],
				[__("Download"), () => this.open_object(item.key, "attachment")],
				[__("Copy temporary link"), () => this.copy_temporary_link(item)],
				[__("Properties"), () => this.show_properties(item)],
				[__("Rename"), () => this.show_rename_dialog(item)],
				[__("Copy"), () => this.show_transfer_dialog(item, "copy")],
				[__("Move"), () => this.show_transfer_dialog(item, "move")],
				...(item.linked ? [[__("Open S3 Vault File"), () => frappe.set_route("Form", "S3 Vault File", item.linked.storage_file)]] : []),
				[__("Delete"), () => this.show_delete_dialog(item), "danger"],
			];

		const dialog = new frappe.ui.Dialog({ title: item.name, fields: [{ fieldtype: "HTML", fieldname: "actions" }] });
		const $container = dialog.fields_dict.actions.$wrapper.addClass("s3fm-action-dialog");
		for (const [label, callback, style] of actions) {
			$(`<button class="btn ${style === "danger" ? "btn-danger" : "btn-default"}">${this.escape(label)}</button>`)
				.appendTo($container).on("click", () => { dialog.hide(); callback(); });
		}
		dialog.show();
	}

	async show_properties(item) {
		try {
			if (item.type === "folder") {
				const summary = await this.call("get_folder_summary", { connection: this.connection, prefix: item.key });
				const warning = summary.truncated ? `<div class="alert alert-warning">${__("The folder is larger than the preview limit. Counts and size are minimum values.")}</div>` : "";
				frappe.msgprint({
					title: __("Folder Properties"),
					message: `${warning}<div class="s3fm-properties-grid"><strong>${__("Folder")}</strong><span>${this.escape(item.name)}</span><strong>${__("Path")}</strong><span>${this.escape(item.key)}</span><strong>${__("Objects")}</strong><span>${summary.truncated ? __("At least ") : ""}${summary.object_count}</span><strong>${__("Size")}</strong><span>${this.format_bytes(summary.total_bytes)}</span><strong>${__("Linked attachments")}</strong><span>${summary.linked_count}</span></div>`,
				});
				return;
			}
			const details = await this.call("get_object_details", { connection: this.connection, key: item.key });
			const metadata = Object.entries(details.metadata || {}).map(([key, value]) => `${this.escape(key)}=${this.escape(value)}`).join("<br>") || "—";
			const tags = (details.tags || []).map((tag) => `${this.escape(tag.Key)}=${this.escape(tag.Value)}`).join("<br>") || "—";
			const linked = details.linked ? `${this.escape(details.linked.storage_file)}<br>${this.escape(details.linked.attached_to_doctype || "")} ${this.escape(details.linked.attached_to_name || "")}` : __("No");
			frappe.msgprint({ title: __("File Properties"), message: `<div class="s3fm-properties-grid"><strong>${__("Name")}</strong><span>${this.escape(details.name)}</span><strong>${__("Key")}</strong><span>${this.escape(details.key)}</span><strong>${__("Size")}</strong><span>${this.format_bytes(details.size)}</span><strong>${__("Content type")}</strong><span>${this.escape(details.content_type)}</span><strong>${__("Modified")}</strong><span>${this.format_datetime(details.last_modified)}</span><strong>ETag</strong><span>${this.escape(details.etag || "—")}</span><strong>${__("Version ID")}</strong><span>${this.escape(details.version_id || "—")}</span><strong>${__("Encryption")}</strong><span>${this.escape(details.server_side_encryption || "—")}</span><strong>${__("Metadata")}</strong><span>${metadata}</span><strong>${__("Tags")}</strong><span>${tags}</span><strong>${__("Managed attachment")}</strong><span>${linked}</span></div>` });
		} catch (error) { this.handle_error(error); }
	}

	async copy_temporary_link(item) {
		try {
			const result = await this.call("get_object_url", { connection: this.connection, key: item.key, disposition: "inline" });
			if (navigator.clipboard && window.isSecureContext) {
				await navigator.clipboard.writeText(result.url);
			} else {
				const input = $("<textarea>").val(result.url).appendTo(document.body).select();
				document.execCommand("copy");
				input.remove();
			}
			frappe.show_alert({ message: __("Temporary link copied; it expires in {0} seconds.", [result.expires_in]), indicator: "green" });
		} catch (error) { this.handle_error(error); }
	}

	show_rename_dialog(item) {
		const dialog = new frappe.ui.Dialog({
			title: __("Rename {0}", [item.type === "folder" ? __("Folder") : __("File")]),
			fields: [
				{ fieldname: "new_name", fieldtype: "Data", label: __("New Name"), default: item.name, reqd: 1 },
				{ fieldname: "conflict_strategy", fieldtype: "Select", label: __("If destination exists"), options: `${__("Stop")}\n${__("Keep both")}`, default: __("Stop") },
				...(item.type === "file" && item.linked ? [{ fieldname: "update_linked_record", fieldtype: "Check", label: __("Update linked S3 Vault File record"), default: 1 }] : []),
			],
			primary_action_label: __("Rename"),
			primary_action: async (values) => {
				dialog.disable_primary_action();
				try {
					if (item.type === "file") {
						await this.call("rename_file", { connection: this.connection, key: item.key, new_name: values.new_name, conflict_strategy: this.conflict_value(values.conflict_strategy), update_linked_record: values.update_linked_record ?? 1 }, true);
						frappe.show_alert({ message: __("File renamed"), indicator: "green" });
					} else {
						await this.start_background_operation({ operation_type: "Rename Folder", source_prefix: item.key, new_name: values.new_name, conflict_strategy: this.conflict_value(values.conflict_strategy), update_linked_records: 1 });
					}
					dialog.hide();
					await this.load_current_folder();
				} catch (error) { dialog.enable_primary_action(); this.handle_error(error); }
			},
		});
		dialog.show();
	}

	async show_transfer_dialog(item, mode) {
		const destination = await this.pick_folder(__("Select Destination Folder"), this.current_prefix);
		if (destination === null) return;
		const is_folder = item.type === "folder";
		const dialog = new frappe.ui.Dialog({
			title: mode === "copy" ? __("Copy {0}", [item.name]) : __("Move {0}", [item.name]),
			fields: [
				{ fieldname: "destination", fieldtype: "Data", label: __("Destination Folder"), read_only: 1, default: destination || "/" },
				{ fieldname: "new_name", fieldtype: "Data", label: __("Name at Destination"), default: item.name, reqd: 1 },
				{ fieldname: "conflict_strategy", fieldtype: "Select", label: __("If destination exists"), options: is_folder ? `${__("Stop")}\n${__("Keep both")}` : `${__("Stop")}\n${__("Replace")}\n${__("Keep both")}`, default: __("Stop") },
				...(mode === "move" && item.type === "file" && item.linked ? [{ fieldname: "update_linked_record", fieldtype: "Check", label: __("Update linked S3 Vault File record"), default: 1 }] : []),
			],
			primary_action_label: mode === "copy" ? __("Copy") : __("Move"),
			primary_action: async (values) => {
				dialog.disable_primary_action();
				try {
					if (item.type === "file") {
						await this.call(mode === "copy" ? "copy_file" : "move_file", { connection: this.connection, key: item.key, destination_prefix: destination, new_name: values.new_name, conflict_strategy: this.conflict_value(values.conflict_strategy), update_linked_record: values.update_linked_record ?? 1 }, true);
						frappe.show_alert({ message: mode === "copy" ? __("File copied") : __("File moved"), indicator: "green" });
					} else {
						await this.start_background_operation({ operation_type: mode === "copy" ? "Copy Folder" : "Move Folder", source_prefix: item.key, destination_prefix: destination, new_name: values.new_name, conflict_strategy: this.conflict_value(values.conflict_strategy), update_linked_records: 1 });
					}
					dialog.hide();
					await this.load_current_folder();
				} catch (error) { dialog.enable_primary_action(); this.handle_error(error); }
			},
		});
		dialog.show();
	}

	async show_delete_dialog(item) {
		if (item.type === "file") {
			const dialog = new frappe.ui.Dialog({
				title: __("Delete File"),
				fields: [
					{ fieldtype: "HTML", options: `<div class="alert alert-danger">${__("This action deletes the current S3 object. Bucket versioning may retain older versions.")}</div>` },
					...(item.linked ? [{ fieldtype: "HTML", options: `<div class="alert alert-warning">${__("This is a managed Frappe attachment. Deleting it will make the attachment unavailable.")}</div>` }, { fieldname: "allow_linked_delete", fieldtype: "Check", label: __("I understand and allow linked attachment deletion"), default: 0 }] : []),
					{ fieldname: "confirmation", fieldtype: "Data", label: __("Type the exact file name to confirm"), reqd: 1 },
				],
				primary_action_label: __("Delete File"),
				primary_action: async (values) => {
					if (values.confirmation !== item.name) { frappe.msgprint(__("The confirmation does not match the file name.")); return; }
					if (item.linked && !values.allow_linked_delete) { frappe.msgprint(__("Confirm linked attachment deletion first.")); return; }
					dialog.disable_primary_action();
					try {
						await this.call("delete_file", { connection: this.connection, key: item.key, allow_linked_delete: values.allow_linked_delete || 0, confirmation: values.confirmation }, true);
						dialog.hide();
						frappe.show_alert({ message: __("File deleted"), indicator: "green" });
						await this.load_current_folder();
					} catch (error) { dialog.enable_primary_action(); this.handle_error(error); }
				},
			});
			dialog.show();
			return;
		}

		try {
			const summary = await this.call("get_folder_summary", { connection: this.connection, prefix: item.key });
			const dialog = new frappe.ui.Dialog({
				title: __("Delete Folder"),
				fields: [
					{ fieldtype: "HTML", options: `<div class="alert alert-danger"><strong>${__("This deletes all objects under the folder prefix.")}</strong><br>${summary.truncated ? __("At least ") : ""}${summary.object_count} ${__("objects")}, ${this.format_bytes(summary.total_bytes)}, ${summary.linked_count} ${__("linked attachment record(s)")}.</div>` },
					...(summary.linked_count ? [{ fieldname: "allow_linked_delete", fieldtype: "Check", label: __("I understand and allow deletion of linked attachments"), default: 0 }] : []),
					{ fieldname: "confirmation", fieldtype: "Data", label: __("Type the folder name to confirm"), reqd: 1 },
				],
				primary_action_label: __("Delete Folder"),
				primary_action: async (values) => {
					if (values.confirmation !== item.name) { frappe.msgprint(__("The confirmation does not match the folder name.")); return; }
					if (summary.linked_count && !values.allow_linked_delete) { frappe.msgprint(__("Confirm linked attachment deletion first.")); return; }
					dialog.disable_primary_action();
					try {
						await this.start_background_operation({ operation_type: "Delete Folder", source_prefix: item.key, allow_linked_delete: values.allow_linked_delete || 0, confirmation: values.confirmation });
						dialog.hide();
					} catch (error) { dialog.enable_primary_action(); this.handle_error(error); }
				},
			});
			dialog.show();
		} catch (error) { this.handle_error(error); }
	}

	async start_bulk_transfer(mode) {
		const items = Array.from(this.selected.values());
		if (!items.length) return;
		const destination = await this.pick_folder(__("Select Destination Folder"), this.current_prefix);
		if (destination === null) return;
		const has_folder = items.some((item) => item.type === "folder");
		const allow_replace = !has_folder && items.length === 1;
		const dialog = new frappe.ui.Dialog({
			title: mode === "copy" ? __("Copy Selected Items") : __("Move Selected Items"),
			fields: [
				{ fieldtype: "HTML", options: `<p>${items.length} ${__("selected item(s) will be processed in the background.")}</p>` },
				{ fieldname: "destination", fieldtype: "Data", label: __("Destination Folder"), read_only: 1, default: destination || "/" },
				{ fieldname: "conflict_strategy", fieldtype: "Select", label: __("If destination exists"), options: allow_replace ? `${__("Stop")}\n${__("Replace")}\n${__("Keep both")}` : `${__("Stop")}\n${__("Keep both")}`, default: __("Stop") },
				...(mode === "move" ? [{ fieldname: "update_linked_records", fieldtype: "Check", label: __("Update linked S3 Vault File records"), default: 1 }] : []),
			],
			primary_action_label: mode === "copy" ? __("Start Copy") : __("Start Move"),
			primary_action: async (values) => {
				dialog.disable_primary_action();
				try {
					await this.start_background_operation({ operation_type: mode === "copy" ? "Bulk Copy" : "Bulk Move", items, destination_prefix: destination, conflict_strategy: this.conflict_value(values.conflict_strategy), update_linked_records: values.update_linked_records ?? 1 });
					dialog.hide(); this.clear_selection();
				} catch (error) { dialog.enable_primary_action(); this.handle_error(error); }
			},
		});
		dialog.show();
	}

	async start_bulk_delete() {
		const items = Array.from(this.selected.values());
		if (!items.length) return;
		const linked_count = items.filter((item) => item.linked).length;
		const dialog = new frappe.ui.Dialog({
			title: __("Delete Selected Items"),
			fields: [
				{ fieldtype: "HTML", options: `<div class="alert alert-danger">${__("This background operation recursively deletes selected folders and files.")}<br>${items.length} ${__("selected row(s)")}; ${linked_count} ${__("directly linked file(s) visible on this page")}.</div>` },
				{ fieldname: "allow_linked_delete", fieldtype: "Check", label: __("I understand and allow deletion of linked attachments found inside the selection"), default: 0 },
				{ fieldname: "confirmation", fieldtype: "Data", label: __("Type DELETE to confirm"), reqd: 1 },
			],
			primary_action_label: __("Start Delete"),
			primary_action: async (values) => {
				if (values.confirmation !== "DELETE") { frappe.msgprint(__("Type DELETE exactly.")); return; }
				dialog.disable_primary_action();
				try {
					await this.start_background_operation({ operation_type: "Bulk Delete", items, allow_linked_delete: values.allow_linked_delete || 0, confirmation: "DELETE" });
					dialog.hide(); this.clear_selection();
				} catch (error) { dialog.enable_primary_action(); this.handle_error(error); }
			},
		});
		dialog.show();
	}

	async start_folder_zip(item) {
		try { await this.start_background_operation({ operation_type: "Download Folder ZIP", source_prefix: item.key }); }
		catch (error) { this.handle_error(error); }
	}

	async start_bulk_zip() {
		const items = Array.from(this.selected.values());
		if (!items.length) return;
		try { await this.start_background_operation({ operation_type: "Bulk Download ZIP", items }); this.clear_selection(); }
		catch (error) { this.handle_error(error); }
	}

	async start_background_operation(args) {
		const result = await this.call("create_background_operation", { connection: this.connection, ...args }, true);
		frappe.show_alert({ message: __("Background operation queued"), indicator: "blue" });
		this.operations.unshift(result);
		this.toggle_operations(true);
		this.render_operations();
		this.start_operation_polling();
		return result;
	}

	async pick_folder(title, initial_prefix = "") {
		return new Promise((resolve) => {
			let selected_prefix = initial_prefix || "";
			let settled = false;
			const dialog = new frappe.ui.Dialog({
				title,
				fields: [{ fieldtype: "HTML", fieldname: "browser" }],
				primary_action_label: __("Select This Folder"),
				primary_action: () => { settled = true; dialog.hide(); resolve(selected_prefix); },
			});
			const $browser = dialog.fields_dict.browser.$wrapper.addClass("s3fm-folder-picker");
			const load = async (prefix) => {
				$browser.html(`<div class="text-muted">${__("Loading folders...")}</div>`);
				try {
					const result = await this.call("list_folders", { connection: this.connection, prefix });
					selected_prefix = result.prefix || "";
					const parts = selected_prefix.replace(/\/$/, "").split("/").filter(Boolean);
					let accumulated = "";
					const crumbs = [`<button class="btn btn-default btn-xs" data-picker-prefix="">${__("Root")}</button>`];
					for (const part of parts) { accumulated += `${part}/`; crumbs.push(`<span>/</span><button class="btn btn-default btn-xs" data-picker-prefix="${encodeURIComponent(accumulated)}">${this.escape(part)}</button>`); }
					const folders = (result.folders || []).map((folder) => `<button class="s3fm-picker-folder" data-picker-prefix="${encodeURIComponent(folder.key)}"><span>📁</span><span>${this.escape(folder.name)}</span></button>`).join("") || `<div class="text-muted s3fm-picker-empty">${__("No subfolders")}</div>`;
					const warning = result.is_truncated ? `<div class="alert alert-warning">${__("Only the first 1000 folders are shown.")}</div>` : "";
					$browser.html(`${warning}<div class="s3fm-picker-current"><strong>${__("Selected")}:</strong> ${this.escape(selected_prefix || "/")}</div><div class="s3fm-picker-breadcrumb">${crumbs.join(" ")}</div><div class="s3fm-picker-list">${folders}</div>`);
					$browser.find("[data-picker-prefix]").on("click", (event) => load(decodeURIComponent($(event.currentTarget).attr("data-picker-prefix"))));
				} catch (error) { this.handle_error(error); dialog.hide(); if (!settled) resolve(null); }
			};
			dialog.$wrapper.on("hidden.bs.modal", () => { if (!settled) resolve(null); });
			dialog.show();
			load(selected_prefix);
		});
	}

	conflict_value(label) {
		if (label === __("Replace")) return "replace";
		if (label === __("Keep both")) return "keep_both";
		return "fail";
	}

	async load_recent_operations(show_errors = true) {
		if (!this.connection) return;
		try {
			this.operations = await this.call("get_recent_operations", { connection: this.connection, limit: 12 });
			this.render_operations();
			this.update_operation_badge();
			this.start_operation_polling();
		} catch (error) { if (show_errors) this.handle_error(error); }
	}

	toggle_operations(show) {
		this.operations_visible = Boolean(show);
		this.$root.find(".s3fm-operations-panel").toggleClass("hidden", !this.operations_visible);
		if (this.operations_visible) this.load_recent_operations(false);
	}

	render_operations() {
		const $list = this.$root.find(".s3fm-operation-list");
		if (!this.operations.length) {
			$list.html(`<div class="s3fm-operation-empty text-muted">${__("No operations for this connection yet.")}</div>`);
			return;
		}
		$list.html(this.operations.map((operation) => {
			const progress = Math.max(0, Math.min(Number(operation.progress) || 0, 100));
			const status_class = String(operation.status || "").toLowerCase().replace(/\s+/g, "-");
			const counts = operation.total_objects ? `${operation.processed_objects || 0} / ${operation.total_objects} ${__("objects")}` : __("Preparing");
			const download = operation.status === "Completed" && operation.result_key && !operation.result_deleted ? `<button class="btn btn-primary btn-xs" data-operation-download="${operation.name}">${__("Download")}</button>` : "";
			const error = operation.error_message ? `<div class="s3fm-operation-error">${this.escape(operation.error_message)}</div>` : "";
			return `<div class="s3fm-operation-card"><div class="s3fm-operation-top"><div><strong>${this.escape(operation.operation_type)}</strong><span class="s3fm-status s3fm-status-${status_class}">${this.escape(operation.status)}</span></div><div class="s3fm-operation-card-actions">${download}<button class="btn btn-default btn-xs" data-operation-open="${operation.name}">${__("Details")}</button></div></div><div class="s3fm-operation-path">${this.escape(operation.source_key || "")} ${operation.destination_key ? `→ ${this.escape(operation.destination_key)}` : ""}</div><div class="progress s3fm-operation-progress"><div class="progress-bar" style="width:${progress}%">${Math.round(progress)}%</div></div><div class="s3fm-operation-meta"><span>${counts}</span><span>${this.format_bytes(operation.processed_size || 0)} / ${this.format_bytes(operation.total_size || 0)}</span><span>${this.escape(operation.message || "")}</span></div>${error}</div>`;
		}).join(""));
	}

	update_operation_badge() {
		const running = this.operations.filter((row) => ["Queued", "Running"].includes(row.status)).length;
		const $badge = this.$root.find(".s3fm-operation-count").text(running);
		$badge.toggleClass("hidden", running === 0);
	}

	start_operation_polling() {
		const has_running = this.operations.some((row) => ["Queued", "Running"].includes(row.status));
		if (!has_running) {
			if (this.operation_poll_timer) clearTimeout(this.operation_poll_timer);
			this.operation_poll_timer = null;
			return;
		}
		if (this.operation_poll_timer) return;
		this.operation_poll_timer = setTimeout(async () => {
			this.operation_poll_timer = null;
			await this.load_recent_operations(false);
		}, 3000);
	}

	async download_operation_result(operation_name) {
		try {
			const result = await this.call("get_operation_result_url", { operation_name });
			this.trigger_download(result.url, result.filename || "");
		} catch (error) { this.handle_error(error); }
	}

	trigger_download(url, filename = "") {
		const anchor = document.createElement("a");
		anchor.href = url;
		anchor.rel = "noopener";
		if (filename) anchor.download = filename;
		document.body.appendChild(anchor);
		anchor.click();
		anchor.remove();
	}

	is_duplicate_error(error) {
		const text = [
			error?.message,
			error?._server_messages,
			error?.exc,
		].filter(Boolean).join(" ").toLowerCase();
		return text.includes("already exists") || text.includes("file named");
	}

	confirm_promise(message) {
		return new Promise((resolve) => frappe.confirm(message, () => resolve(true), () => resolve(false)));
	}

	is_previewable(content_type, filename) {
		const type = String(content_type || "").toLowerCase();
		const extension = String(filename || "").split(".").pop().toLowerCase();
		return type.startsWith("image/") || type.startsWith("video/") || type.startsWith("audio/") || type === "application/pdf" || type.startsWith("text/") || ["pdf", "txt", "json", "xml", "csv", "md"].includes(extension);
	}

	file_icon(content_type, filename) {
		const type = String(content_type || "").toLowerCase();
		const extension = String(filename || "").split(".").pop().toLowerCase();
		if (type.startsWith("image/")) return "🖼️";
		if (type.startsWith("video/")) return "🎬";
		if (type.startsWith("audio/")) return "🎵";
		if (type === "application/pdf" || extension === "pdf") return "📕";
		if (["zip", "rar", "7z", "tar", "gz"].includes(extension)) return "🗜️";
		if (["xls", "xlsx", "csv"].includes(extension)) return "📊";
		if (["doc", "docx", "odt"].includes(extension)) return "📘";
		if (["ppt", "pptx"].includes(extension)) return "📙";
		if (type.startsWith("text/") || ["txt", "json", "xml", "md"].includes(extension)) return "📄";
		return "📦";
	}

	format_bytes(value) {
		const bytes = Number(value) || 0;
		if (!bytes) return "0 B";
		const units = ["B", "KB", "MB", "GB", "TB", "PB"];
		const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
		return `${(bytes / Math.pow(1024, index)).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
	}

	format_datetime(value) {
		if (!value) return "—";
		try { return frappe.datetime.str_to_user(value); } catch (error) { return this.escape(value); }
	}

	set_loading(loading) {
		this.$root.find(".s3fm-loading").toggleClass("hidden", !loading);
		this.$root.find(".s3fm-table-wrap").toggleClass("s3fm-is-loading", loading);
	}

	escape(value) {
		if (frappe.utils.escape_html) return frappe.utils.escape_html(String(value || ""));
		return $("<div>").text(String(value || "")).html();
	}

	handle_error(error) {
		console.error(error);
		if (error?.message && !error.exc) {
			frappe.msgprint({ title: __("S3 File Manager"), message: this.escape(error.message), indicator: "red" });
		}
	}
}

/* -------------------------------------------------------------------------
 * Phase 3 enterprise enhancements
 * ------------------------------------------------------------------------- */
(() => {
	const MULTIPART_THRESHOLD = 100 * 1024 * 1024;
	const MULTIPART_CONCURRENCY = 3;
	const PART_URL_BATCH = 2;

	const phase2_on_page_load = frappe.pages["s3-file-manager"].on_page_load;
	frappe.pages["s3-file-manager"].on_page_load = function (wrapper) {
		phase2_on_page_load(wrapper);
		wrapper.s3_file_manager?.enable_phase3();
	};

	const original_load_current_folder = S3FileManager.prototype.load_current_folder;
	S3FileManager.prototype.load_current_folder = async function () {
		this.global_search_active = false;
		await original_load_current_folder.call(this);
		this.apply_phase3_capabilities();
		this.refresh_lazy_tree_selection();
	};

	const original_open_folder = S3FileManager.prototype.open_folder;
	S3FileManager.prototype.open_folder = function (prefix) {
		this.global_search_active = false;
		return original_open_folder.call(this, prefix);
	};

	const original_next = S3FileManager.prototype.go_next_page;
	S3FileManager.prototype.go_next_page = function () {
		if (this.global_search_active) {
			this.global_search_start += this.page_size;
			this.run_global_search();
			return;
		}
		return original_next.call(this);
	};

	const original_previous = S3FileManager.prototype.go_previous_page;
	S3FileManager.prototype.go_previous_page = function () {
		if (this.global_search_active) {
			this.global_search_start = Math.max(0, this.global_search_start - this.page_size);
			this.run_global_search();
			return;
		}
		return original_previous.call(this);
	};

	S3FileManager.prototype.enable_phase3 = function () {
		if (this.phase3_enabled) return;
		this.phase3_enabled = true;
		this.access_roots = [""];
		this.active_root = "";
		this.capabilities = {};
		this.global_search_active = false;
		this.global_search_start = 0;
		this.global_search_total = 0;
		this.tree_loaded = new Set();
		this.multipart_active = new Map();

		this.inject_phase3_controls();
		this.bind_phase3_events();
	};

	S3FileManager.prototype.inject_phase3_controls = function () {
		const root_control = $(`
			<div class="s3fm-access-root-wrap hidden">
				<label>${__("Access Root")}</label>
				<select class="form-control s3fm-access-root"></select>
			</div>
		`);
		this.$root.find(".s3fm-connection-wrap").after(root_control);

		this.$root.find(".s3fm-search-wrap label").text(__("Search"));
		this.$root.find(".s3fm-search-wrap").append(`
			<div class="s3fm-search-options">
				<select class="form-control s3fm-search-scope">
					<option value="page">${__("Current page")}</option>
					<option value="index">${__("Entire indexed bucket")}</option>
				</select>
				<button class="btn btn-default s3fm-search-run">${__("Search")}</button>
			</div>
		`);

		this.$root.find(".s3fm-actions").prepend(`
			<button class="btn btn-default s3fm-dashboard" title="${__("Storage dashboard")}">
				📊 ${__("Dashboard")}
			</button>
			<button class="btn btn-default s3fm-versions" title="${__("Object versions")}">
				🕘 ${__("Versions")}
			</button>
			<button class="btn btn-default s3fm-resumable-uploads" title="${__("Resumable uploads")}">
				${frappe.utils.icon("upload", "sm")} ${__("Uploads")}
			</button>
			<button class="btn btn-default s3fm-upload-folder" title="${__("Upload a complete folder")}">
				${frappe.utils.icon("folder", "sm")} ${__("Upload Folder")}
			</button>
			<input type="file" class="s3fm-folder-input" webkitdirectory directory multiple hidden>
		`);

		this.$root.find(".s3fm-shell").before(`
			<div class="s3fm-drop-zone hidden">
				<div class="s3fm-drop-zone-inner">
					<div class="s3fm-drop-icon">☁️</div>
					<strong>${__("Drop files or folders here")}</strong>
					<span>${__("They will upload to the currently open S3 folder.")}</span>
				</div>
			</div>
		`);
	};

	S3FileManager.prototype.bind_phase3_events = function () {
		this.$root.on("change", ".s3fm-access-root", (event) => {
			this.active_root = event.target.value || "";
			this.current_prefix = this.active_root;
			this.reset_pagination();
			this.clear_selection();
			this.reset_lazy_tree();
			this.load_current_folder();
		});
		this.$root.on("change", ".s3fm-search-scope", () => {
			this.global_search_start = 0;
			if (this.$root.find(".s3fm-search-scope").val() === "page") {
				this.global_search_active = false;
				this.load_current_folder();
			}
		});
		this.$root.on("click", ".s3fm-search-run", () => {
			if (this.$root.find(".s3fm-search-scope").val() === "index") this.run_global_search();
			else this.render_items();
		});
		this.$root.on("keydown", ".s3fm-search", (event) => {
			if (event.key === "Enter" && this.$root.find(".s3fm-search-scope").val() === "index") {
				event.preventDefault();
				this.global_search_start = 0;
				this.run_global_search();
			}
		});
		this.$root.on("click", ".s3fm-dashboard", () => this.show_storage_dashboard());
		this.$root.on("click", ".s3fm-versions", () => this.show_versions_dialog(null));
		this.$root.on("click", ".s3fm-resumable-uploads", () => this.show_resumable_uploads());
		this.$root.on("click", ".s3fm-upload-folder", () => this.$root.find(".s3fm-folder-input").trigger("click"));
		this.$root.on("change", ".s3fm-folder-input", (event) => {
			this.upload_files_phase3(event.target.files, true);
			event.target.value = "";
		});
		this.$root.on("click", "[data-tree-toggle]", (event) => {
			event.stopPropagation();
			const prefix = decodeURIComponent($(event.currentTarget).attr("data-tree-toggle"));
			this.toggle_tree_node(prefix, $(event.currentTarget).closest(".s3fm-tree-node"));
		});
		this.$root.on("click", "[data-tree-open]", (event) => {
			const prefix = decodeURIComponent($(event.currentTarget).attr("data-tree-open"));
			this.open_folder(prefix);
		});
		this.$root.on("click", "[data-operation-cancel]", async (event) => {
			try {
				await this.call("cancel_operation", { operation_name: $(event.currentTarget).attr("data-operation-cancel") }, true);
				await this.load_recent_operations(false);
			} catch (error) { this.handle_error(error); }
		});
		this.$root.on("click", "[data-operation-retry]", async (event) => {
			try {
				await this.call("retry_operation", { operation_name: $(event.currentTarget).attr("data-operation-retry") }, true);
				await this.load_recent_operations(false);
			} catch (error) { this.handle_error(error); }
		});

		const shell = this.$root.find(".s3fm-shell")[0];
		let drag_depth = 0;
		$(shell).on("dragenter", (event) => {
			event.preventDefault(); drag_depth += 1;
			if (this.capabilities.upload) this.$root.find(".s3fm-drop-zone").removeClass("hidden");
		});
		$(shell).on("dragover", (event) => event.preventDefault());
		$(shell).on("dragleave", (event) => {
			event.preventDefault(); drag_depth = Math.max(0, drag_depth - 1);
			if (!drag_depth) this.$root.find(".s3fm-drop-zone").addClass("hidden");
		});
		$(shell).on("drop", async (event) => {
			event.preventDefault(); drag_depth = 0;
			this.$root.find(".s3fm-drop-zone").addClass("hidden");
			if (!this.capabilities.upload) return;
			const files = await this.files_from_drop(event.originalEvent.dataTransfer);
			this.upload_files_phase3(files, true);
		});
	};

	S3FileManager.prototype.connection_row = function () {
		return this.connection_rows.find((row) => row.name === this.connection) || {};
	};

	S3FileManager.prototype.configure_access_roots = function () {
		const row = this.connection_row();
		this.access_roots = row.access_roots?.length ? row.access_roots : [""];
		if (!this.access_roots.some((root) => this.current_prefix === root || this.current_prefix.startsWith(root))) {
			this.current_prefix = row.default_root ?? this.access_roots[0] ?? "";
		}
		this.active_root = this.access_roots.find((root) => this.current_prefix === root || this.current_prefix.startsWith(root)) ?? this.access_roots[0] ?? "";
		const $control = this.$root.find(".s3fm-access-root-wrap");
		const $select = this.$root.find(".s3fm-access-root");
		$select.html(this.access_roots.map((root) => `<option value="${this.escape(root)}">${this.escape(root || "/")}</option>`).join(""));
		$select.val(this.active_root);
		$control.toggleClass("hidden", this.access_roots.length <= 1);
		this.connection_control.df.get_query = () => ({ filters: { name: ["in", this.connection_rows.map((item) => item.name)] } });
	};

	const p2_load_connections = S3FileManager.prototype.load_connections;
	S3FileManager.prototype.load_connections = async function () {
		await p2_load_connections.call(this);
		if (this.connection) {
			this.configure_access_roots();
			const row = this.connection_row();
			const expected = row.default_root ?? this.access_roots[0] ?? "";
			if (expected && !this.current_prefix.startsWith(expected)) {
				this.current_prefix = expected;
				await this.load_current_folder();
			}
			this.reset_lazy_tree();
		}
	};

	const p2_connection_change = S3FileManager.prototype.on_connection_change;
	S3FileManager.prototype.on_connection_change = async function () {
		await p2_connection_change.call(this);
		if (this.connection) {
			this.configure_access_roots();
			const expected = this.connection_row().default_root ?? this.access_roots[0] ?? "";
			if (this.current_prefix !== expected) {
				this.current_prefix = expected;
				await this.load_current_folder();
			}
			this.reset_lazy_tree();
		}
	};

	S3FileManager.prototype.render_breadcrumb = function () {
		if (this.global_search_active) {
			this.$root.find(".s3fm-breadcrumb").html(`<strong>${__("Indexed Search Results")}</strong><span class="text-muted">${this.global_search_total} ${__("matching rows")}</span>`);
			return;
		}
		const root = this.active_root || "";
		const relative = this.current_prefix.startsWith(root) ? this.current_prefix.slice(root.length) : this.current_prefix;
		const parts = relative.replace(/\/$/, "").split("/").filter(Boolean);
		let accumulated = root;
		const crumbs = [`<button class="s3fm-crumb" data-breadcrumb-prefix="${encodeURIComponent(root)}">${frappe.utils.icon("home", "sm")} ${__("Root")}</button>`];
		for (const part of parts) {
			accumulated += `${part}/`;
			crumbs.push(`<span class="s3fm-crumb-separator">/</span>`);
			crumbs.push(`<button class="s3fm-crumb" data-breadcrumb-prefix="${encodeURIComponent(accumulated)}">${this.escape(part)}</button>`);
		}
		this.$root.find(".s3fm-breadcrumb").html(crumbs.join(""));
	};

	S3FileManager.prototype.apply_phase3_capabilities = function () {
		this.capabilities = this.data.capabilities || this.capabilities || {};
		const c = this.capabilities;
		this.$root.find(".s3fm-upload, .s3fm-upload-folder").toggleClass("hidden", !c.upload);
		this.$root.find(".s3fm-new-folder").toggleClass("hidden", !c.create_folder);
		this.$root.find(".s3fm-bulk-copy").toggleClass("hidden", !c.copy);
		this.$root.find(".s3fm-bulk-move").toggleClass("hidden", !c.move);
		this.$root.find(".s3fm-bulk-delete").toggleClass("hidden", !c.delete);
		this.$root.find(".s3fm-bulk-zip").toggleClass("hidden", !c.zip);
		this.$root.find(".s3fm-dashboard").toggleClass("hidden", !c.dashboard);
		this.$root.find(".s3fm-versions").toggleClass("hidden", !c.versions_view);
		this.$root.find(".s3fm-resumable-uploads").toggleClass("hidden", !c.upload);
	};

	S3FileManager.prototype.render_sidebar = function () {
		this.render_lazy_tree();
	};

	S3FileManager.prototype.reset_lazy_tree = function () {
		this.tree_loaded = new Set();
		this.render_lazy_tree();
	};

	S3FileManager.prototype.render_lazy_tree = function () {
		const $list = this.$root.find(".s3fm-folder-list");
		if (!$list.length || !this.connection) return;
		$list.empty().addClass("s3fm-tree");
		if (this.access_roots.length > 1) {
			for (const root of this.access_roots) {
				const name = root.replace(/\/$/, "").split("/").pop() || __("Root");
				$list.append(this.tree_node_html({ name, key: root }, true));
			}
		} else {
			const root = this.active_root || this.access_roots[0] || "";
			const $root_children = $('<div class="s3fm-tree-root-children"></div>').appendTo($list);
			this.load_tree_children(root, $root_children);
		}
	};

	S3FileManager.prototype.tree_node_html = function (folder, is_root = false) {
		return $(`
			<div class="s3fm-tree-node" data-tree-node="${encodeURIComponent(folder.key)}">
				<div class="s3fm-tree-row">
					<button class="s3fm-tree-toggle" data-tree-toggle="${encodeURIComponent(folder.key)}" title="${__("Expand")}">▸</button>
					<button class="s3fm-tree-open" data-tree-open="${encodeURIComponent(folder.key)}"><span>📁</span><span>${this.escape(folder.name)}</span></button>
				</div>
				<div class="s3fm-tree-children hidden"></div>
			</div>
		`);
	};

	S3FileManager.prototype.toggle_tree_node = async function (prefix, $node) {
		const $children = $node.children(".s3fm-tree-children");
		const opening = $children.hasClass("hidden");
		$children.toggleClass("hidden", !opening);
		$node.children(".s3fm-tree-row").find(".s3fm-tree-toggle").text(opening ? "▾" : "▸");
		if (opening && !this.tree_loaded.has(prefix)) await this.load_tree_children(prefix, $children);
	};

	S3FileManager.prototype.load_tree_children = async function (prefix, $container) {
		$container.html(`<div class="s3fm-tree-loading text-muted">${__("Loading...")}</div>`);
		try {
			const result = await this.call("list_tree_children", { connection: this.connection, prefix });
			this.tree_loaded.add(prefix);
			const folders = result.folders || [];
			if (!folders.length) {
				$container.html(`<div class="s3fm-tree-empty text-muted">${__("No subfolders")}</div>`);
				return;
			}
			$container.empty();
			for (const folder of folders) $container.append(this.tree_node_html(folder));
			this.refresh_lazy_tree_selection();
		} catch (error) {
			$container.html(`<div class="text-danger">${__("Could not load folders")}</div>`);
		}
	};

	S3FileManager.prototype.refresh_lazy_tree_selection = function () {
		this.$root.find(".s3fm-tree-row").removeClass("s3fm-tree-current");
		this.$root.find(`[data-tree-node="${encodeURIComponent(this.current_prefix || "")}"] > .s3fm-tree-row`).addClass("s3fm-tree-current");
	};

	S3FileManager.prototype.pick_folder = function (title, initial_prefix = "") {
		return new Promise((resolve) => {
			let selected = initial_prefix || this.active_root || "";
			let settled = false;
			const dialog = new frappe.ui.Dialog({
				title,
				fields: [
					{ fieldname: "access_root", fieldtype: "Select", label: __("Access Root"), options: this.access_roots.map((root) => root || "/").join("\n"), default: selected || "/", hidden: this.access_roots.length <= 1 },
					{ fieldname: "browser", fieldtype: "HTML" },
				],
				primary_action_label: __("Select This Folder"),
				primary_action: () => { settled = true; dialog.hide(); resolve(selected); },
			});
			const $browser = dialog.fields_dict.browser.$wrapper.addClass("s3fm-folder-picker");
			const load = async (prefix) => {
				$browser.html(`<div class="text-muted">${__("Loading folders...")}</div>`);
				try {
					const result = await this.call("list_folders", { connection: this.connection, prefix });
					selected = result.prefix || "";
					const root = this.access_roots.find((item) => selected === item || selected.startsWith(item)) ?? this.active_root ?? "";
					const relative = selected.startsWith(root) ? selected.slice(root.length) : selected;
					const parts = relative.replace(/\/$/, "").split("/").filter(Boolean);
					let accumulated = root;
					const crumbs = [`<button class="btn btn-default btn-xs" data-picker-prefix="${encodeURIComponent(root)}">${__("Root")}</button>`];
					for (const part of parts) { accumulated += `${part}/`; crumbs.push(`<span>/</span><button class="btn btn-default btn-xs" data-picker-prefix="${encodeURIComponent(accumulated)}">${this.escape(part)}</button>`); }
					const folders = (result.folders || []).map((folder) => `<button class="s3fm-picker-folder" data-picker-prefix="${encodeURIComponent(folder.key)}"><span>📁</span><span>${this.escape(folder.name)}</span></button>`).join("") || `<div class="text-muted s3fm-picker-empty">${__("No subfolders")}</div>`;
					$browser.html(`<div class="s3fm-picker-current"><strong>${__("Selected")}:</strong> ${this.escape(selected || "/")}</div><div class="s3fm-picker-breadcrumb">${crumbs.join(" ")}</div><div class="s3fm-picker-list">${folders}</div>`);
					$browser.find("[data-picker-prefix]").on("click", (event) => load(decodeURIComponent($(event.currentTarget).attr("data-picker-prefix"))));
				} catch (error) { this.handle_error(error); dialog.hide(); if (!settled) resolve(null); }
			};
			dialog.fields_dict.access_root?.$input?.on("change", (event) => {
				const label = event.target.value;
				load(label === "/" ? "" : label);
			});
			dialog.$wrapper.on("hidden.bs.modal", () => { if (!settled) resolve(null); });
			dialog.show();
			load(selected);
		});
	};

	S3FileManager.prototype.run_global_search = async function () {
		if (!this.connection) return;
		const query = String(this.$root.find(".s3fm-search").val() || "").trim();
		this.set_loading(true);
		try {
			const result = await this.call_module("frappe_s3_vault.file_manager_index.search_index", {
				connection: this.connection,
				query,
				start: this.global_search_start || 0,
				page_length: this.page_size,
				include_folders: 1,
			});
			this.global_search_active = true;
			this.global_search_total = result.total || 0;
			this.data = {
				folders: (result.rows || []).filter((row) => row.type === "folder"),
				files: (result.rows || []).filter((row) => row.type === "file"),
				capabilities: this.capabilities,
			};
			this.render_breadcrumb();
			this.render_items();
			this.$root.find(".s3fm-page-info").text(__("{0}-{1} of {2}", [
				Math.min(this.global_search_start + 1, this.global_search_total),
				Math.min(this.global_search_start + this.page_size, this.global_search_total),
				this.global_search_total,
			]));
			this.$root.find(".s3fm-previous").prop("disabled", this.global_search_start <= 0);
			this.$root.find(".s3fm-next").prop("disabled", this.global_search_start + this.page_size >= this.global_search_total);
			if (!result.index_configured) frappe.show_alert({ message: __("Create and build an object index before using global search."), indicator: "orange" });
		} catch (error) { this.handle_error(error); }
		finally { this.set_loading(false); }
	};

	S3FileManager.prototype.call_module = async function (method, args = {}, freeze = false) {
		const response = await frappe.call({ method, args, freeze, freeze_message: freeze ? __("Processing S3 request...") : undefined });
		return response.message || {};
	};

	S3FileManager.prototype.show_storage_dashboard = async function () {
		try {
			const result = await this.call_module("frappe_s3_vault.file_manager_index.get_dashboard", { connection: this.connection }, true);
			const summary = result.summary || {};
			const categories = (result.categories || []).map((row) => `<tr><td>${this.escape(row.category)}</td><td>${row.object_count || 0}</td><td>${this.format_bytes(row.total_size || 0)}</td></tr>`).join("") || `<tr><td colspan="3" class="text-muted">${__("No indexed objects")}</td></tr>`;
			const largest = (result.largest || []).map((row) => `<tr><td title="${this.escape(row.relative_key)}">${this.escape(row.file_name)}</td><td>${this.format_bytes(row.size || 0)}</td><td>${this.format_datetime(row.last_modified)}</td></tr>`).join("") || `<tr><td colspan="3" class="text-muted">${__("No indexed objects")}</td></tr>`;
			const dialog = new frappe.ui.Dialog({ title: __("S3 Storage Dashboard"), size: "extra-large", fields: [{ fieldtype: "HTML", fieldname: "content" }] });
			dialog.fields_dict.content.$wrapper.html(`
				<div class="s3fm-dashboard-cards">
					<div><strong>${summary.object_count || 0}</strong><span>${__("Objects")}</span></div>
					<div><strong>${summary.folder_count || 0}</strong><span>${__("Folders")}</span></div>
					<div><strong>${this.format_bytes(summary.total_size || 0)}</strong><span>${__("Indexed Size")}</span></div>
					<div><strong>${this.format_datetime(summary.indexed_on)}</strong><span>${__("Last Indexed")}</span></div>
				</div>
				<div class="s3fm-dashboard-grid">
					<div><h5>${__("By File Type")}</h5><table class="table"><thead><tr><th>${__("Category")}</th><th>${__("Count")}</th><th>${__("Size")}</th></tr></thead><tbody>${categories}</tbody></table></div>
					<div><h5>${__("Largest Objects")}</h5><table class="table"><thead><tr><th>${__("Name")}</th><th>${__("Size")}</th><th>${__("Modified")}</th></tr></thead><tbody>${largest}</tbody></table></div>
				</div>
				<div class="s3fm-dashboard-footer"><span>${__("Index status")}: <strong>${this.escape(result.configuration?.status || __("Not configured"))}</strong></span>${this.capabilities.index_rebuild ? `<button class="btn btn-primary s3fm-rebuild-index">${__("Rebuild Index")}</button>` : ""}</div>
			`);
			dialog.fields_dict.content.$wrapper.find(".s3fm-rebuild-index").on("click", async () => {
				try {
					const operation = await this.call_module("frappe_s3_vault.file_manager_index.start_rebuild", { connection: this.connection }, true);
					dialog.hide(); this.operations.unshift(operation); this.toggle_operations(true); this.render_operations(); this.start_operation_polling();
				} catch (error) { this.handle_error(error); }
			});
			dialog.show();
		} catch (error) { this.handle_error(error); }
	};

	S3FileManager.prototype.show_versions_dialog = async function (item) {
		const key = item?.type === "file" ? item.key : null;
		const prefix = key ? null : this.current_prefix;
		try {
			const status = await this.call_module("frappe_s3_vault.file_manager_versions.get_versioning_status", { connection: this.connection });
			const dialog = new frappe.ui.Dialog({ title: key ? __("Versions of {0}", [item.name]) : __("Versions in Current Folder"), size: "extra-large", fields: [{ fieldtype: "HTML", fieldname: "content" }] });
			const $content = dialog.fields_dict.content.$wrapper;
			$content.html(`<div class="alert ${status.status === "Enabled" ? "alert-success" : "alert-warning"}">${__("Bucket versioning")}: <strong>${this.escape(status.status || __("Unknown"))}</strong>${status.error ? `<br>${this.escape(status.error)}` : ""}</div><div class="s3fm-version-list text-muted">${__("Loading versions...")}</div>`);
			dialog.show();
			const load = async (markers = {}) => {
				const result = await this.call_module("frappe_s3_vault.file_manager_versions.list_versions", { connection: this.connection, key, prefix, max_keys: 100, ...markers });
				const rows = result.rows || [];
				const html = rows.map((row) => {
					const encoded = encodeURIComponent(JSON.stringify(row));
					const type = row.type === "delete_marker" ? __("Delete marker") : __("Version");
					const current = row.is_latest ? `<span class="indicator-pill green">${__("Latest")}</span>` : "";
					return `<tr><td>${this.escape(row.key)}</td><td>${type} ${current}</td><td>${this.format_bytes(row.size || 0)}</td><td>${this.format_datetime(row.last_modified)}</td><td><button class="btn btn-default btn-xs" data-version-actions="${encoded}">${__("Actions")}</button></td></tr>`;
				}).join("") || `<tr><td colspan="5" class="text-muted">${__("No versions or delete markers found.")}</td></tr>`;
				$content.find(".s3fm-version-list").html(`<div class="s3fm-version-table"><table class="table"><thead><tr><th>${__("Key")}</th><th>${__("Type")}</th><th>${__("Size")}</th><th>${__("Modified")}</th><th></th></tr></thead><tbody>${html}</tbody></table></div>${result.is_truncated ? `<button class="btn btn-default s3fm-more-versions">${__("Load More")}</button>` : ""}`);
				$content.find("[data-version-actions]").on("click", (event) => this.show_version_actions(JSON.parse(decodeURIComponent($(event.currentTarget).attr("data-version-actions"))), dialog));
				$content.find(".s3fm-more-versions").on("click", () => load({ key_marker: result.next_key_marker, version_id_marker: result.next_version_id_marker }));
			};
			await load();
		} catch (error) { this.handle_error(error); }
	};

	S3FileManager.prototype.show_version_actions = function (row, parent_dialog) {
		const actions = [];
		if (row.type === "version") {
			actions.push([__("Preview"), () => this.open_version(row, "inline")]);
			actions.push([__("Download"), () => this.open_version(row, "attachment")]);
			if (this.capabilities.versions_restore) actions.push([__("Restore as Current Version"), () => this.restore_version(row, parent_dialog)]);
		} else if (this.capabilities.versions_restore) {
			actions.push([__("Remove Delete Marker / Restore"), () => this.remove_delete_marker(row, parent_dialog)]);
		}
		if (this.capabilities.versions_delete) actions.push([__("Permanently Delete This Version"), () => this.permanent_delete_version(row, parent_dialog), "danger"]);
		const dialog = new frappe.ui.Dialog({ title: row.version_id, fields: [{ fieldtype: "HTML", fieldname: "actions" }] });
		for (const [label, callback, style] of actions) $(`<button class="btn ${style === "danger" ? "btn-danger" : "btn-default"}">${this.escape(label)}</button>`).appendTo(dialog.fields_dict.actions.$wrapper.addClass("s3fm-action-dialog")).on("click", () => { dialog.hide(); callback(); });
		dialog.show();
	};

	S3FileManager.prototype.open_version = async function (row, disposition) {
		let win = disposition === "inline" ? window.open("about:blank", "_blank") : null;
		try {
			const result = await this.call_module("frappe_s3_vault.file_manager_versions.get_version_url", { connection: this.connection, key: row.key, version_id: row.version_id, disposition });
			if (win) win.location.replace(result.url); else this.trigger_download(result.url, result.filename || "");
		} catch (error) { if (win) win.close(); this.handle_error(error); }
	};

	S3FileManager.prototype.restore_version = async function (row, parent) {
		const confirmed = await this.confirm_promise(__("Restore this historical version as the current object?"));
		if (!confirmed) return;
		try { await this.call_module("frappe_s3_vault.file_manager_versions.restore_version", { connection: this.connection, key: row.key, version_id: row.version_id }, true); parent.hide(); await this.load_current_folder(); frappe.show_alert({ message: __("Version restored"), indicator: "green" }); }
		catch (error) { this.handle_error(error); }
	};

	S3FileManager.prototype.remove_delete_marker = async function (row, parent) {
		const confirmed = await this.confirm_promise(__("Remove this delete marker and make the previous object version visible again?"));
		if (!confirmed) return;
		try { await this.call_module("frappe_s3_vault.file_manager_versions.remove_delete_marker", { connection: this.connection, key: row.key, version_id: row.version_id }, true); parent.hide(); await this.load_current_folder(); }
		catch (error) { this.handle_error(error); }
	};

	S3FileManager.prototype.permanent_delete_version = function (row, parent) {
		const dialog = new frappe.ui.Dialog({ title: __("Permanent Version Deletion"), fields: [{ fieldtype: "HTML", options: `<div class="alert alert-danger">${__("This permanently deletes one S3 version and cannot be undone.")}</div>` }, { fieldname: "confirmation", fieldtype: "Data", label: __("Type PERMANENT DELETE"), reqd: 1 }], primary_action_label: __("Permanently Delete"), primary_action: async (values) => {
			if (values.confirmation !== "PERMANENT DELETE") return frappe.msgprint(__("Type PERMANENT DELETE exactly."));
			try { await this.call_module("frappe_s3_vault.file_manager_versions.permanently_delete_version", { connection: this.connection, key: row.key, version_id: row.version_id, confirmation: values.confirmation }, true); dialog.hide(); parent.hide(); await this.load_current_folder(); }
			catch (error) { this.handle_error(error); }
		} });
		dialog.show();
	};

	const p2_show_item_actions = S3FileManager.prototype.show_item_actions;
	S3FileManager.prototype.show_item_actions = function (item) {
		if (!item) return;
		const c = item.capabilities || this.capabilities || {};
		const actions = [];
		if (item.type === "folder") {
			actions.push([__("Open"), () => this.open_folder(item.key)]);
			actions.push([__("Properties / Size"), () => this.show_properties(item)]);
			if (c.rename) actions.push([__("Rename"), () => this.show_rename_dialog(item)]);
			if (c.copy) actions.push([__("Copy"), () => this.show_transfer_dialog(item, "copy")]);
			if (c.move) actions.push([__("Move"), () => this.show_transfer_dialog(item, "move")]);
			if (c.zip) actions.push([__("Download ZIP"), () => this.start_folder_zip(item)]);
			if (c.delete) actions.push([__("Delete"), () => this.show_delete_dialog(item), "danger"]);
		} else {
			if (c.preview) actions.push([__("Preview"), () => this.open_object(item.key, "inline")]);
			if (c.download) actions.push([__("Download"), () => this.open_object(item.key, "attachment")]);
			if (c.download) actions.push([__("Copy temporary link"), () => this.copy_temporary_link(item)]);
			actions.push([__("Properties"), () => this.show_properties(item)]);
			if (c.versions_view) actions.push([__("Versions"), () => this.show_versions_dialog(item)]);
			if (c.rename) actions.push([__("Rename"), () => this.show_rename_dialog(item)]);
			if (c.copy) actions.push([__("Copy"), () => this.show_transfer_dialog(item, "copy")]);
			if (c.move) actions.push([__("Move"), () => this.show_transfer_dialog(item, "move")]);
			if (item.linked) actions.push([__("Open S3 Vault File"), () => frappe.set_route("Form", "S3 Vault File", item.linked.storage_file)]);
			if (c.delete) actions.push([__("Delete"), () => this.show_delete_dialog(item), "danger"]);
		}
		const dialog = new frappe.ui.Dialog({ title: item.name, fields: [{ fieldtype: "HTML", fieldname: "actions" }] });
		const $container = dialog.fields_dict.actions.$wrapper.addClass("s3fm-action-dialog");
		for (const [label, callback, style] of actions) $(`<button class="btn ${style === "danger" ? "btn-danger" : "btn-default"}">${this.escape(label)}</button>`).appendTo($container).on("click", () => { dialog.hide(); callback(); });
		dialog.show();
	};

	const p2_render_operations = S3FileManager.prototype.render_operations;
	S3FileManager.prototype.render_operations = function () {
		const $list = this.$root.find(".s3fm-operation-list");
		if (!this.operations.length) { $list.html(`<div class="s3fm-operation-empty text-muted">${__("No operations for this connection yet.")}</div>`); return; }
		$list.html(this.operations.map((operation) => {
			const progress = Math.max(0, Math.min(Number(operation.progress) || 0, 100));
			const status_class = String(operation.status || "").toLowerCase().replace(/\s+/g, "-");
			const counts = operation.total_objects ? `${operation.processed_objects || 0} / ${operation.total_objects} ${__("objects")}` : __("Preparing");
			const download = operation.status === "Completed" && operation.result_key && !operation.result_deleted ? `<button class="btn btn-primary btn-xs" data-operation-download="${operation.name}">${__("Download")}</button>` : "";
			const cancel = ["Queued", "Running"].includes(operation.status) && this.capabilities.operation_cancel ? `<button class="btn btn-default btn-xs" data-operation-cancel="${operation.name}">${operation.cancellation_requested ? __("Cancelling...") : __("Cancel")}</button>` : "";
			const retry = ["Failed", "Partially Completed", "Cancelled"].includes(operation.status) && this.capabilities.operation_retry ? `<button class="btn btn-default btn-xs" data-operation-retry="${operation.name}">${__("Retry")}</button>` : "";
			const error = operation.error_message ? `<div class="s3fm-operation-error">${this.escape(operation.error_message)}</div>` : "";
			return `<div class="s3fm-operation-card"><div class="s3fm-operation-top"><div><strong>${this.escape(operation.operation_type)}</strong><span class="s3fm-status s3fm-status-${status_class}">${this.escape(operation.status)}</span></div><div class="s3fm-operation-card-actions">${download}${cancel}${retry}<button class="btn btn-default btn-xs" data-operation-open="${operation.name}">${__("Details")}</button></div></div><div class="s3fm-operation-path">${this.escape(operation.source_key || "")} ${operation.destination_key ? `→ ${this.escape(operation.destination_key)}` : ""}</div><div class="progress s3fm-operation-progress"><div class="progress-bar" style="width:${progress}%">${Math.round(progress)}%</div></div><div class="s3fm-operation-meta"><span>${counts}</span><span>${this.format_bytes(operation.processed_size || 0)} / ${this.format_bytes(operation.total_size || 0)}</span><span>${this.escape(operation.message || "")}</span></div>${error}</div>`;
		}).join(""));
	};

	S3FileManager.prototype.upload_files = function (file_list) {
		return this.upload_files_phase3(file_list, false);
	};

	S3FileManager.prototype.upload_files_phase3 = async function (file_list, preserve_paths = false) {
		const files = Array.from(file_list || []).filter((file) => file.size > 0);
		this.$root.find(".s3fm-file-input, .s3fm-folder-input").val("");
		if (!files.length || !this.connection || !this.capabilities.upload) return;
		try {
			for (let index = 0; index < files.length; index++) {
				let strategy = "fail";
				const file = files[index];
				const relative_path = preserve_paths ? (file.s3RelativePath || file.webkitRelativePath || file.name) : file.name;
				this.show_upload_progress(0, __("Preparing {0} ({1} of {2})", [relative_path, index + 1, files.length]));
				try {
					await this.upload_one_phase3(file, relative_path, strategy, index + 1, files.length);
				} catch (error) {
					if (!this.is_duplicate_error(error)) throw error;
					strategy = await this.choose_upload_conflict(file.name);
					if (strategy === "cancel") throw new Error(__("Upload cancelled."));
					await this.upload_one_phase3(file, relative_path, strategy, index + 1, files.length);
				}
			}
			this.hide_upload_progress();
			frappe.show_alert({ message: __("Upload completed"), indicator: "green" });
			await this.load_current_folder();
		} catch (error) { this.hide_upload_progress(); this.handle_error(error); }
	};

	S3FileManager.prototype.upload_one_phase3 = async function (file, relative_path, strategy, number, total) {
		if (file.size >= MULTIPART_THRESHOLD) return this.upload_multipart(file, relative_path, strategy, number, total);
		const session = await this.call("create_upload_session", {
			connection: this.connection, prefix: this.current_prefix, filename: file.name,
			content_type: file.type || "application/octet-stream", file_size: file.size,
			relative_path, conflict_strategy: strategy,
		});
		await this.put_file(file, session, number, total);
		await this.call("complete_upload", { connection: this.connection, key: session.key, expected_size: file.size });
	};

	S3FileManager.prototype.choose_upload_conflict = function (filename) {
		return new Promise((resolve) => {
			const dialog = new frappe.ui.Dialog({ title: __("File Already Exists"), fields: [{ fieldtype: "HTML", options: `<p>${__("A destination object already exists for {0}.", [this.escape(filename)])}</p>` }] });
			const $footer = dialog.$wrapper.find(".modal-footer").empty();
			$(`<button class="btn btn-default">${__("Cancel")}</button>`).appendTo($footer).on("click", () => { dialog.hide(); resolve("cancel"); });
			$(`<button class="btn btn-default">${__("Keep both")}</button>`).appendTo($footer).on("click", () => { dialog.hide(); resolve("keep_both"); });
			$(`<button class="btn btn-primary">${__("Replace")}</button>`).appendTo($footer).on("click", () => { dialog.hide(); resolve("replace"); });
			dialog.show();
		});
	};

	S3FileManager.prototype.file_fingerprint = function (file, relative_path = "") {
		return `${file.name}:${file.size}:${file.lastModified || 0}:${relative_path || ""}`;
	};

	S3FileManager.prototype.upload_multipart = async function (file, relative_path, strategy, number, total) {
		let session = await this.call_module("frappe_s3_vault.file_manager_multipart.create_session", {
			connection: this.connection, prefix: this.current_prefix, filename: file.name,
			file_size: file.size, content_type: file.type || "application/octet-stream",
			relative_path, file_fingerprint: this.file_fingerprint(file, relative_path), conflict_strategy: strategy,
		});
		const uploaded = new Set((session.parts || []).map((row) => Number(row.PartNumber)));
		let uploaded_bytes = Number(session.uploaded_size || 0);
		const pending = [];
		for (let part = 1; part <= session.total_parts; part++) if (!uploaded.has(part)) pending.push(part);
		let cursor = 0;
		const worker = async () => {
			while (cursor < pending.length) {
				const batch = pending.slice(cursor, cursor + PART_URL_BATCH); cursor += batch.length;
				const urls = await this.call_module("frappe_s3_vault.file_manager_multipart.get_part_urls", { session_name: session.name, part_numbers: batch });
				for (const row of urls.parts || []) {
					const part_number = Number(row.part_number);
					const start = (part_number - 1) * session.part_size;
					const end = Math.min(file.size, start + session.part_size);
					await this.put_part_with_retry(row.url, file.slice(start, end), 3);
					uploaded_bytes += end - start;
					const percent = Math.round((uploaded_bytes / file.size) * 100);
					this.show_upload_progress(percent, __("Multipart upload {0} ({1} of {2})", [relative_path, number, total]));
				}
			}
		};
		await Promise.all(Array.from({ length: Math.min(MULTIPART_CONCURRENCY, Math.max(1, pending.length)) }, () => worker()));
		return this.call_module("frappe_s3_vault.file_manager_multipart.complete_session", { session_name: session.name });
	};

	S3FileManager.prototype.put_part_with_retry = async function (url, blob, retries) {
		let last_error;
		for (let attempt = 1; attempt <= retries; attempt++) {
			try {
				const response = await fetch(url, { method: "PUT", body: blob });
				if (!response.ok) throw new Error(__("Multipart part upload failed with HTTP {0}.", [response.status]));
				return;
			} catch (error) { last_error = error; if (attempt < retries) await new Promise((resolve) => setTimeout(resolve, attempt * 1000)); }
		}
		throw last_error;
	};

	S3FileManager.prototype.show_resumable_uploads = async function () {
		try {
			const rows = await this.call_module("frappe_s3_vault.file_manager_multipart.list_resumable_uploads", { connection: this.connection });
			const dialog = new frappe.ui.Dialog({ title: __("Resumable Multipart Uploads"), size: "large", fields: [{ fieldtype: "HTML", fieldname: "content" }] });
			const html = rows.map((row) => `<tr><td>${this.escape(row.file_name)}</td><td>${this.escape(row.relative_key)}</td><td>${row.uploaded_parts || 0}/${row.total_parts || 0}</td><td>${this.format_bytes(row.uploaded_size || 0)} / ${this.format_bytes(row.file_size || 0)}</td><td><button class="btn btn-primary btn-xs" data-resume-upload="${row.name}">${__("Resume")}</button> <button class="btn btn-default btn-xs" data-abort-upload="${row.name}">${__("Abort")}</button></td></tr>`).join("") || `<tr><td colspan="5" class="text-muted">${__("No active multipart uploads.")}</td></tr>`;
			dialog.fields_dict.content.$wrapper.html(`<table class="table"><thead><tr><th>${__("File")}</th><th>${__("Destination")}</th><th>${__("Parts")}</th><th>${__("Progress")}</th><th></th></tr></thead><tbody>${html}</tbody></table>`);
			dialog.fields_dict.content.$wrapper.find("[data-abort-upload]").on("click", async (event) => { await this.call_module("frappe_s3_vault.file_manager_multipart.abort_session", { session_name: $(event.currentTarget).attr("data-abort-upload") }, true); dialog.hide(); this.show_resumable_uploads(); });
			dialog.fields_dict.content.$wrapper.find("[data-resume-upload]").on("click", (event) => {
				const session_name = $(event.currentTarget).attr("data-resume-upload");
				const session = rows.find((row) => row.name === session_name);
				const input = document.createElement("input"); input.type = "file";
				input.onchange = async () => {
					const file = input.files?.[0]; if (!file) return;
					const expected_fingerprint_start = `${file.name}:${file.size}:${file.lastModified || 0}:`;
					if (file.size !== Number(session.file_size) || (session.file_fingerprint && !session.file_fingerprint.startsWith(expected_fingerprint_start))) return frappe.msgprint(__("Select the same local file (same name, size, and modified time) to resume."));
					dialog.hide();
					try { await this.resume_multipart_file(file, session); await this.load_current_folder(); } catch (error) { this.handle_error(error); }
				};
				input.click();
			});
			dialog.show();
		} catch (error) { this.handle_error(error); }
	};

	S3FileManager.prototype.resume_multipart_file = async function (file, session) {
		const refreshed = await this.call_module("frappe_s3_vault.file_manager_multipart.refresh_session", { session_name: session.name });
		const uploaded = new Set((refreshed.parts || []).map((row) => Number(row.PartNumber)));
		let uploaded_bytes = Number(refreshed.uploaded_size || 0);
		for (let part = 1; part <= refreshed.total_parts; part++) {
			if (uploaded.has(part)) continue;
			const urls = await this.call_module("frappe_s3_vault.file_manager_multipart.get_part_urls", { session_name: refreshed.name, part_numbers: [part] });
			const start = (part - 1) * refreshed.part_size;
			const end = Math.min(file.size, start + refreshed.part_size);
			await this.put_part_with_retry(urls.parts[0].url, file.slice(start, end), 3);
			uploaded_bytes += end - start;
			this.show_upload_progress(Math.round(uploaded_bytes / file.size * 100), __("Resuming {0}", [file.name]));
		}
		await this.call_module("frappe_s3_vault.file_manager_multipart.complete_session", { session_name: refreshed.name });
		this.hide_upload_progress();
		frappe.show_alert({ message: __("Multipart upload completed"), indicator: "green" });
	};

	S3FileManager.prototype.files_from_drop = async function (data_transfer) {
		const items = Array.from(data_transfer?.items || []);
		if (!items.length || !items[0].webkitGetAsEntry) return Array.from(data_transfer?.files || []);
		const output = [];
		const read_entry = async (entry, path = "") => {
			if (entry.isFile) {
				await new Promise((resolve, reject) => entry.file((file) => { file.s3RelativePath = `${path}${file.name}`; output.push(file); resolve(); }, reject));
				return;
			}
			if (entry.isDirectory) {
				const reader = entry.createReader();
				let entries = [];
				while (true) {
					const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
					if (!batch.length) break;
					entries = entries.concat(batch);
				}
				for (const child of entries) await read_entry(child, `${path}${entry.name}/`);
			}
		};
		for (const item of items) { const entry = item.webkitGetAsEntry(); if (entry) await read_entry(entry); }
		return output;
	};
})();
