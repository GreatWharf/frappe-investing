/* Frappe Investing — Broker Connection form script.
 *
 * Broker-conditional credential fields, native grouped custom buttons, a
 * dirty-form guard before every API call, and an honest headline per status
 * (Zerodha tokens expire daily — reconnect each trading day).
 */
frappe.ui.form.on("Broker Connection", {
	refresh(frm) {
		toggleCredentialFields(frm);
		setStatusHeadline(frm);

		if (frm.is_new()) return; // keep the native Save action on new documents

		const saved = () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save your changes first."));
				return false;
			}
			return true;
		};

		const syncGroup = __("Sync");
		const viewGroup = __("View");

		if (frm.doc.broker === "Zerodha") {
			frm.add_custom_button(__("Connect"), () => {
				if (!saved()) return;
				zerodhaConnect(frm);
			}, syncGroup);
		}

		if (frm.doc.enabled) {
			let syncing = false;
			frm.add_custom_button(__("Sync Now"), async () => {
				if (!saved() || syncing) return;
				syncing = true;
				try {
					await frappe.call({
						method: "frappe_investing.api.sync_now",
						args: { connection: frm.doc.name },
						freeze: true,
						freeze_message: __("Queuing sync…"),
					});
					frappe.show_alert({
						message: __("Sync queued. It runs in the background; check Broker Sync Logs for progress."),
						indicator: "green",
					});
				} finally {
					syncing = false;
				}
			});
			frm.change_custom_button_type(__("Sync Now"), null, "primary");
		}

		frm.add_custom_button(__("View Sync Logs"), () => {
			frappe.set_route("List", "Broker Sync Log", { connection: frm.doc.name });
		}, viewGroup);

		frm.add_custom_button(__("View Events"), () => {
			// Investment Event has no direct connection link; its `source`
			// carries the broker name, so this is the closest native filter.
			frappe.set_route("List", "Investment Event", { source: frm.doc.broker });
		}, viewGroup);

		frm.add_custom_button(__("Broker Setup"), () => {
			frappe.set_route("broker-setup");
		}, viewGroup);
	},

	broker(frm) {
		toggleCredentialFields(frm);
		setStatusHeadline(frm);
	},

	status(frm) {
		setStatusHeadline(frm);
	},
});

function toggleCredentialFields(frm) {
	const broker = frm.doc.broker;
	const show = {
		api_key: broker === "Zerodha" || broker === "Alpaca",
		api_secret: broker === "Zerodha" || broker === "Alpaca",
		access_token: broker === "Zerodha",
		flex_token: broker === "Interactive Brokers",
		flex_query_id: broker === "Interactive Brokers",
		sandbox: broker === "Alpaca",
	};
	for (const [fieldname, visible] of Object.entries(show)) {
		frm.toggle_display(fieldname, visible);
	}
	if (broker === "Zerodha") {
		frm.set_df_property(
			"access_token",
			"description",
			__("Filled by the Zerodha login flow. Kite tokens expire daily — reconnect each trading day.")
		);
	}
}

function setStatusHeadline(frm) {
	if (!frm.doc) return;
	const status = frm.doc.status;
	if (status === "Token Expired") {
		frm.dashboard.set_headline_alert(
			frm.doc.broker === "Zerodha"
				? __("Daily Zerodha tokens expire; reconnect each trading day before syncing. Use Connect above.")
				: __("The broker token has expired. Reconnect before syncing."),
			"orange"
		);
	} else if (status === "Error") {
		frm.dashboard.set_headline_alert(
			__("Last sync failed: {0}", [frm.doc.last_error || __("see Broker Sync Logs")]),
			"red"
		);
	} else if (status === "Not Connected" && !frm.is_new()) {
		frm.dashboard.set_headline_alert(
			frm.doc.broker === "CSV Import"
				? __("CSV Import has no automatic sync; upload files through Import Batches.")
				: __("Connect and enable this connection to start syncing."),
			"blue"
		);
	} else {
		frm.dashboard.set_headline_alert("");
	}
}

async function zerodhaConnect(frm) {
	let url;
	try {
		const r = await frappe.call({
			method: "frappe_investing.api.zerodha_login_url",
			args: { connection: frm.doc.name },
			freeze: true,
			freeze_message: __("Preparing the Kite login…"),
		});
		url = r.message.url;
	} catch (error) {
		frappe.msgprint(__("Could not start the Zerodha login. Check the API key and secret."));
		return;
	}
	if (window.open) window.open(url, "_blank", "noopener");
	const esc =
		frappe.utils && frappe.utils.escape_html
			? frappe.utils.escape_html
			: (v) => String(v);
	const dialog = new frappe.ui.Dialog({
		title: __("Complete Zerodha Login"),
		fields: [
			{ fieldname: "help", fieldtype: "HTML",
				options: `<p class="text-muted">${esc(
					__("Finish logging in on the Kite tab, then paste the request_token from the redirect URL here. Kite tokens expire daily.")
				)}</p>` },
			{ fieldname: "request_token", label: __("Request Token"), fieldtype: "Data", reqd: 1 },
		],
		primary_action_label: __("Connect"),
		primary_action: async (values) => {
			const token = (values.request_token || "").trim();
			if (!token) {
				frappe.throw(__("Paste the request token from the Kite redirect URL."));
				return;
			}
			try {
				await frappe.call({
					method: "frappe_investing.api.zerodha_exchange_token",
					args: { connection: frm.doc.name, request_token: token },
					freeze: true,
					freeze_message: __("Exchanging token…"),
				});
				dialog.hide();
				frappe.show_alert({ message: __("Zerodha connected for today."), indicator: "green" });
				frm.reload_doc();
			} catch (error) {
				frappe.throw(__("Token exchange failed. Check the request token and try again."));
			}
		},
	});
	dialog.show();
}
