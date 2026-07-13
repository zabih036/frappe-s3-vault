frappe.pages["s3-file-manager"].on_page_load = function (wrapper) {
	new S3FileManager(wrapper);
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

		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("S3 File Manager"),
			single_column: true,
		});

		this.make_layout();
		this.bind_events();
		this.load_connections();
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
								<div class="text-muted">
									${__("Upload a file or create a folder to begin.")}
								</div>
							</div>
						</div>

						<div class="s3fm-pagination">
							<button class="btn btn-default btn-sm s3fm-previous">
								${__("Previous")}
							</button>
							<span class="s3fm-page-info"></span>
							<button class="btn btn-default btn-sm s3fm-next">
								${__("Next")}
							</button>
						</div>
					</section>
				</div>

				<div class="s3fm-no-connection hidden">
					<div class="s3fm-empty-icon">☁️</div>
					<h4>${__("No enabled S3 connection found")}</h4>
					<p class="text-muted">
						${__("Create and enable an S3 Vault Bucket connection first.")}
					</p>
					<button class="btn btn-primary s3fm-open-connections">
						${__("Open S3 Vault Buckets")}
					</button>
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
				get_query: () => ({
					filters: { enabled: 1 },
				}),
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
			this.load_current_folder();
		});
		this.$root.on("click", ".s3fm-previous", () => this.go_previous_page());
		this.$root.on("click", ".s3fm-next", () => this.go_next_page());
		this.$root.on("click", ".s3fm-open-connections", () => {
			frappe.set_route("List", "S3 Vault Bucket");
		});

		this.$root.on("click", "[data-folder-key]", (event) => {
			const key = decodeURIComponent($(event.currentTarget).attr("data-folder-key"));
			this.open_folder(key);
		});

		this.$root.on("click", "[data-breadcrumb-prefix]", (event) => {
			const prefix = decodeURIComponent(
				$(event.currentTarget).attr("data-breadcrumb-prefix")
			);
			this.open_folder(prefix);
		});

		this.$root.on("click", "[data-preview-key]", (event) => {
			event.stopPropagation();
			const key = decodeURIComponent($(event.currentTarget).attr("data-preview-key"));
			this.open_object(key, "inline");
		});

		this.$root.on("click", "[data-download-key]", (event) => {
			event.stopPropagation();
			const key = decodeURIComponent($(event.currentTarget).attr("data-download-key"));
			this.open_object(key, "attachment");
		});
	}

	async call(method, args = {}, freeze = false) {
		const response = await frappe.call({
			method: `${this.api}.${method}`,
			args,
			freeze,
			freeze_message: freeze ? __("Loading S3 data...") : undefined,
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

			const current_value = this.connection_control.get_value();
			const default_connection =
				current_value || result.default_connection || this.connection_rows[0].name;

			await this.connection_control.set_value(default_connection);
			this.connection = default_connection;
			this.render_connection_summary();
			await this.load_current_folder();
		} catch (error) {
			this.handle_error(error);
		}
	}

	async on_connection_change() {
		const value = this.connection_control.get_value();
		if (!value || value === this.connection) {
			return;
		}

		this.connection = value;
		this.current_prefix = "";
		this.reset_pagination();
		this.$root.find(".s3fm-search").val("");
		this.render_connection_summary();
		await this.load_current_folder();
	}

	render_connection_summary() {
		const row = this.connection_rows.find((item) => item.name === this.connection);
		if (!row) {
			this.$root.find(".s3fm-connection-summary").empty();
			return;
		}

		const prefix = row.base_prefix
			? `<span><strong>${__("Virtual root")}:</strong> ${this.escape(row.base_prefix)}</span>`
			: `<span><strong>${__("Virtual root")}:</strong> /</span>`;

		this.$root.find(".s3fm-connection-summary").html(`
			<span><strong>${__("Bucket")}:</strong> ${this.escape(row.bucket_name)}</span>
			<span><strong>${__("Provider")}:</strong> ${this.escape(row.provider_type || "")}</span>
			<span><strong>${__("Region")}:</strong> ${this.escape(row.region || "")}</span>
			${prefix}
		`);
	}

	show_no_connection() {
		this.$root.find(".s3fm-toolbar, .s3fm-connection-summary, .s3fm-breadcrumb, .s3fm-shell")
			.addClass("hidden");
		this.$root.find(".s3fm-no-connection").removeClass("hidden");
	}

	hide_no_connection() {
		this.$root.find(".s3fm-toolbar, .s3fm-connection-summary, .s3fm-breadcrumb, .s3fm-shell")
			.removeClass("hidden");
		this.$root.find(".s3fm-no-connection").addClass("hidden");
	}

	reset_pagination() {
		this.current_token = null;
		this.previous_tokens = [];
		this.next_token = null;
	}

	async load_current_folder() {
		if (!this.connection) {
			return;
		}

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
		this.$root.find(".s3fm-search").val("");
		this.load_current_folder();
	}

	go_next_page() {
		if (!this.next_token) {
			return;
		}

		this.previous_tokens.push(this.current_token);
		this.current_token = this.next_token;
		this.load_current_folder();
	}

	go_previous_page() {
		if (!this.previous_tokens.length) {
			return;
		}

		this.current_token = this.previous_tokens.pop() || null;
		this.load_current_folder();
	}

	render_breadcrumb() {
		const parts = (this.current_prefix || "").replace(/\/$/, "").split("/").filter(Boolean);
		let accumulated = "";
		const crumbs = [
			`<button class="s3fm-crumb" data-breadcrumb-prefix="">${frappe.utils.icon("home", "sm")} ${__("Root")}</button>`,
		];

		for (const part of parts) {
			accumulated += `${part}/`;
			crumbs.push(`<span class="s3fm-crumb-separator">/</span>`);
			crumbs.push(`
				<button
					class="s3fm-crumb"
					data-breadcrumb-prefix="${encodeURIComponent(accumulated)}"
				>${this.escape(part)}</button>
			`);
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

		$list.html(
			folders
				.map(
					(folder) => `
						<button
							class="s3fm-folder-link"
							data-folder-key="${encodeURIComponent(folder.key)}"
							title="${this.escape(folder.name)}"
						>
							<span class="s3fm-folder-icon">📁</span>
							<span>${this.escape(folder.name)}</span>
						</button>
					`
				)
				.join("")
		);
	}

	filtered_items() {
		const query = String(this.$root.find(".s3fm-search").val() || "")
			.trim()
			.toLowerCase();

		const items = [
			...(this.data.folders || []),
			...(this.data.files || []),
		];

		if (!query) {
			return items;
		}

		return items.filter((item) => {
			return (
				String(item.name || "").toLowerCase().includes(query) ||
				String(item.key || "").toLowerCase().includes(query) ||
				String(item.content_type || "").toLowerCase().includes(query)
			);
		});
	}

	render_items() {
		const items = this.filtered_items();
		const $tbody = this.$root.find(".s3fm-table tbody");
		const $empty = this.$root.find(".s3fm-empty");

		if (!items.length) {
			$tbody.empty();
			$empty.removeClass("hidden");
			return;
		}

		$empty.addClass("hidden");
		$tbody.html(items.map((item) => this.render_row(item)).join(""));
	}

	render_row(item) {
		if (item.type === "folder") {
			return `
				<tr class="s3fm-row s3fm-folder-row" data-folder-key="${encodeURIComponent(item.key)}">
					<td>
						<div class="s3fm-name-cell">
							<span class="s3fm-item-icon">📁</span>
							<div>
								<div class="s3fm-item-name">${this.escape(item.name)}</div>
								<div class="s3fm-item-key">${this.escape(item.key)}</div>
							</div>
						</div>
					</td>
					<td class="s3fm-type-column">${__("Folder")}</td>
					<td class="s3fm-size-column">—</td>
					<td class="s3fm-date-column">—</td>
					<td class="s3fm-action-column">
						<button
							class="btn btn-default btn-xs"
							data-folder-key="${encodeURIComponent(item.key)}"
						>${__("Open")}</button>
					</td>
				</tr>
			`;
		}

		const previewable = this.is_previewable(item.content_type, item.name);
		const preview_button = previewable
			? `
				<button
					class="btn btn-default btn-xs"
					data-preview-key="${encodeURIComponent(item.key)}"
				>${__("Preview")}</button>
			`
			: "";

		return `
			<tr class="s3fm-row s3fm-file-row">
				<td>
					<div class="s3fm-name-cell">
						<span class="s3fm-item-icon">${this.file_icon(item.content_type, item.name)}</span>
						<div>
							<div class="s3fm-item-name">${this.escape(item.name)}</div>
							<div class="s3fm-item-key">${this.escape(item.key)}</div>
						</div>
					</div>
				</td>
				<td class="s3fm-type-column">
					${this.escape(item.content_type || __("File"))}
				</td>
				<td class="s3fm-size-column">${this.format_bytes(item.size)}</td>
				<td class="s3fm-date-column">${this.format_datetime(item.last_modified)}</td>
				<td class="s3fm-action-column">
					<div class="s3fm-row-actions">
						${preview_button}
						<button
							class="btn btn-default btn-xs"
							data-download-key="${encodeURIComponent(item.key)}"
						>${__("Download")}</button>
					</div>
				</td>
			</tr>
		`;
	}

	render_pagination() {
		const page_number = this.previous_tokens.length + 1;
		this.$root.find(".s3fm-page-info").text(
			__("Page {0}", [page_number])
		);
		this.$root.find(".s3fm-previous").prop("disabled", !this.previous_tokens.length);
		this.$root.find(".s3fm-next").prop("disabled", !this.next_token);
	}

	async show_new_folder_dialog() {
		if (!this.connection) {
			frappe.msgprint(__("Select an S3 connection first."));
			return;
		}

		const dialog = new frappe.ui.Dialog({
			title: __("Create Folder"),
			fields: [
				{
					fieldname: "location",
					fieldtype: "Data",
					label: __("Current Location"),
					read_only: 1,
					default: this.current_prefix || "/",
				},
				{
					fieldname: "folder_name",
					fieldtype: "Data",
					label: __("Folder Name"),
					reqd: 1,
				},
			],
			primary_action_label: __("Create"),
			primary_action: async (values) => {
				dialog.disable_primary_action();

				try {
					await this.call("create_folder", {
						connection: this.connection,
						prefix: this.current_prefix,
						folder_name: values.folder_name,
					});
					dialog.hide();
					frappe.show_alert({
						message: __("Folder created"),
						indicator: "green",
					});
					await this.load_current_folder();
				} catch (error) {
					dialog.enable_primary_action();
					this.handle_error(error);
				}
			},
		});

		dialog.show();
	}

	async upload_files(file_list) {
		const files = Array.from(file_list || []);
		this.$root.find(".s3fm-file-input").val("");

		if (!files.length || !this.connection) {
			return;
		}

		try {
			for (let index = 0; index < files.length; index++) {
				const file = files[index];
				this.show_upload_progress(
					0,
					__("Preparing {0} ({1} of {2})", [file.name, index + 1, files.length])
				);

				const session = await this.call("create_upload_session", {
					connection: this.connection,
					prefix: this.current_prefix,
					filename: file.name,
					content_type: file.type || "application/octet-stream",
					file_size: file.size,
					overwrite: 0,
				});

				await this.put_file(file, session, index + 1, files.length);

				await this.call("complete_upload", {
					connection: this.connection,
					key: session.key,
					expected_size: file.size,
				});
			}

			this.hide_upload_progress();
			frappe.show_alert({
				message: __("Upload completed"),
				indicator: "green",
			});
			await this.load_current_folder();
		} catch (error) {
			this.hide_upload_progress();
			this.handle_error(error);
		}
	}

	put_file(file, session, file_number, total_files) {
		return new Promise((resolve, reject) => {
			const xhr = new XMLHttpRequest();
			xhr.open(session.method || "PUT", session.upload_url, true);

			Object.entries(session.headers || {}).forEach(([name, value]) => {
				xhr.setRequestHeader(name, value);
			});

			xhr.upload.onprogress = (event) => {
				if (!event.lengthComputable) {
					return;
				}

				const percent = Math.round((event.loaded / event.total) * 100);
				this.show_upload_progress(
					percent,
					__("Uploading {0} ({1} of {2})", [file.name, file_number, total_files])
				);
			};

			xhr.onload = () => {
				if (xhr.status >= 200 && xhr.status < 300) {
					resolve();
					return;
				}

				reject(
					new Error(
						__("S3 upload failed with HTTP status {0}. Check bucket CORS settings.", [
							xhr.status,
						])
					)
				);
			};

			xhr.onerror = () => {
				reject(
					new Error(
						__("The browser could not upload to S3. Check CORS, endpoint, and network access.")
					)
				);
			};

			xhr.send(file);
		});
	}

	show_upload_progress(percent, title) {
		const value = Math.max(0, Math.min(Number(percent) || 0, 100));
		const $status = this.$root.find(".s3fm-upload-status");
		$status.removeClass("hidden");
		$status.find(".s3fm-upload-title").text(title || __("Uploading..."));
		$status
			.find(".progress-bar")
			.css("width", `${value}%`)
			.attr("aria-valuenow", value)
			.text(`${value}%`);
	}

	hide_upload_progress() {
		this.$root.find(".s3fm-upload-status").addClass("hidden");
	}

	async open_object(key, disposition) {
		let preview_window = null;

		if (disposition === "inline") {
			preview_window = window.open("about:blank", "_blank");
		}

		try {
			const result = await this.call("get_object_url", {
				connection: this.connection,
				key,
				disposition,
			});

			if (disposition === "inline") {
				if (preview_window) {
					preview_window.location.replace(result.url);
				} else {
					window.open(result.url, "_blank", "noopener");
				}
				return;
			}

			const anchor = document.createElement("a");
			anchor.href = result.url;
			anchor.rel = "noopener";
			anchor.download = result.name || "";
			document.body.appendChild(anchor);
			anchor.click();
			anchor.remove();
		} catch (error) {
			if (preview_window) {
				preview_window.close();
			}
			this.handle_error(error);
		}
	}

	is_previewable(content_type, filename) {
		const type = String(content_type || "").toLowerCase();
		const extension = String(filename || "").split(".").pop().toLowerCase();

		return (
			type.startsWith("image/") ||
			type.startsWith("video/") ||
			type.startsWith("audio/") ||
			type === "application/pdf" ||
			type.startsWith("text/") ||
			["pdf", "txt", "json", "xml", "csv", "md"].includes(extension)
		);
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
		if (type.startsWith("text/") || ["txt", "json", "xml", "md"].includes(extension)) {
			return "📄";
		}
		return "📦";
	}

	format_bytes(value) {
		const bytes = Number(value) || 0;
		if (!bytes) return "0 B";

		const units = ["B", "KB", "MB", "GB", "TB"];
		const index = Math.min(
			Math.floor(Math.log(bytes) / Math.log(1024)),
			units.length - 1
		);
		const amount = bytes / Math.pow(1024, index);
		return `${amount.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
	}

	format_datetime(value) {
		if (!value) return "—";

		try {
			return frappe.datetime.str_to_user(value);
		} catch (error) {
			return this.escape(value);
		}
	}

	set_loading(loading) {
		this.$root.find(".s3fm-loading").toggleClass("hidden", !loading);
		this.$root.find(".s3fm-table-wrap").toggleClass("s3fm-is-loading", loading);
	}

	escape(value) {
		if (frappe.utils.escape_html) {
			return frappe.utils.escape_html(String(value || ""));
		}
		return $("<div>").text(String(value || "")).html();
	}

	handle_error(error) {
		console.error(error);

		if (error && error.message && !error.exc) {
			frappe.msgprint({
				title: __("S3 File Manager"),
				message: this.escape(error.message),
				indicator: "red",
			});
		}
	}
}
