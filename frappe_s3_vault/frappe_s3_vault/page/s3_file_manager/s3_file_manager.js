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
		this.current_prefix = "";
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
