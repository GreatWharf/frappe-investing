/* Frappe Investing — guided broker connector page (page route: broker-setup).
 *
 * Honest capability badges come straight from PRODUCT_BRIEF.md: what each
 * connector truly syncs, and what it cannot do. Connections are created and
 * edited through the standard Broker Connection form route; this page only
 * guides, connects (Zerodha daily-token flow) and links.
 */
(() => {
	const inv = (frappe.investing = frappe.investing || {});

	const BROKERS = [
		{
			broker: "Zerodha",
			title: __("Zerodha Kite Connect"),
			desc: __(
				"Holdings, today's orders/trades and quotes via the Kite API. Access tokens expire daily — reconnect each trading day."
			),
			badges: [
				{ label: __("Holdings ✓"), tone: "yes" },
				{ label: __("Quotes ✓"), tone: "yes" },
				{ label: __("Today's trades ✓"), tone: "yes" },
				{ label: __("History via CSV"), tone: "warn" },
				{ label: __("No dividend feed"), tone: "warn" },
			],
			note: __(
				"The Kite API has no historical trades and no dividend feed. Import Zerodha Console tradebook and P&L exports through CSV Import for history, dividends and statutory charges."
			),
		},
		{
			broker: "Alpaca",
			title: __("Alpaca"),
			desc: __("US equities. Positions plus account activities — fills, dividends and splits."),
			badges: [
				{ label: __("Positions ✓"), tone: "yes" },
				{ label: __("Trades ✓"), tone: "yes" },
				{ label: __("Dividends ✓"), tone: "yes" },
				{ label: __("Splits ✓"), tone: "yes" },
				{ label: __("US equities only"), tone: "warn" },
			],
			note: __("Uses your API key and secret. Tick Sandbox on the connection for paper trading; it is off by default."),
		},
		{
			broker: "Interactive Brokers",
			title: __("Interactive Brokers Flex"),
			desc: __("Trades, cash activity (dividends, withholding tax, fees) and corporate actions via Flex Web Service."),
			badges: [
				{ label: __("Trades ✓"), tone: "yes" },
				{ label: __("Dividends & WHT ✓"), tone: "yes" },
				{ label: __("Corporate actions ✓"), tone: "yes" },
				{ label: __("History ✓"), tone: "yes" },
			],
			note: __(
				"Two-step fetch: create a Flex token and a saved Flex query in IBKR Client Portal first, then enter the token and the query ID on the connection. Sync requests the query, waits for IBKR to build it, and downloads the report."
			),
		},
		{
			broker: "CSV Import",
			title: __("CSV Import"),
			desc: __("Any event type from any broker, template-driven. Every batch runs as a dry run before you commit it."),
			badges: [
				{ label: __("Any event type ✓"), tone: "yes" },
				{ label: __("Dry run first ✓"), tone: "yes" },
				{ label: __("Manual file upload"), tone: "warn" },
			],
			note: __("Required for Zerodha history and dividends, and for any broker without an API connection."),
			csv: true,
		},
	];

	frappe.pages["broker-setup"] = frappe.pages["broker-setup"] || {};

	frappe.pages["broker-setup"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Broker Setup"),
			single_column: true,
		});
		const root = $('<div class="inv-root"></div>').appendTo(page.main);
		const state = { connections: [], loading: null, exchanging: {} };
		wrapper.broker_setup_state = state;

		const call = async (method, args = {}) =>
			(await frappe.call({ method: `frappe_investing.api.${method}`, args })).message;

		async function load() {
			if (state.loading) return state.loading;
			state.loading = (async () => {
				try {
					const r = await frappe.call({
						method: "frappe.client.get_list",
						args: {
							doctype: "Broker Connection",
							fields: JSON.stringify([
								"name", "connection_name", "broker", "status", "enabled", "last_sync",
							]),
							order_by: "modified desc",
							limit_page_length: 100,
						},
					});
					state.connections = r.message || [];
				} catch (error) {
					state.connections = [];
				} finally {
					state.loading = null;
				}
				render();
			})();
			return state.loading;
		}
		wrapper.broker_setup_reload = load;

		function render() {
			root.empty();
			// Plain muted intro (no card frame) — the page title already names it.
			$('<p class="inv-page-note">')
				.text(
					__("The broker is account truth: connectors normalize broker data into immutable Investment Events. Prices come from your configured market-data provider or broker quotes.")
				)
				.appendTo(root);

			const grid = $('<div class="inv-broker-grid">').appendTo(root);
			for (const spec of BROKERS) renderCard(grid, spec);
		}

		function renderCard(grid, spec) {
			const card = $('<section class="inv-broker-card">').appendTo(grid);
			$("<h2>").text(spec.title).appendTo(card);
			const badges = $('<div class="inv-badges">').appendTo(card);
			for (const badge of spec.badges) {
				$('<span class="inv-badge">')
					.addClass(`indicator-pill ${badge.tone === "yes" ? "green" : "gray"}`)
					.addClass(badge.tone === "yes" ? "inv-badge-yes" : "inv-badge-warn")
					.text(badge.label)
					.appendTo(badges);
			}
			$('<p class="inv-broker-desc">').text(spec.desc).appendTo(card);
			$('<p class="inv-broker-desc">').text(spec.note).appendTo(card);

			const actions = $('<div class="inv-broker-actions">').appendTo(card);
			if (spec.csv) {
				$('<button type="button" class="btn btn-default">')
					.text(__("Open Import Batches"))
					.on("click", () => frappe.set_route("List", "Import Batch"))
					.appendTo(actions);
			}
			$('<button type="button" class="btn btn-primary">')
				.text(spec.csv ? __("New CSV Connection") : __("New {0} Connection", [spec.broker]))
				.on("click", () => {
					if (frappe.new_doc) {
						frappe.new_doc("Broker Connection", { broker: spec.broker });
					} else {
						frappe.set_route("Form", "Broker Connection", "new-broker-connection-1");
					}
				})
				.appendTo(actions);

			const existing = state.connections.filter((c) => c.broker === spec.broker);
			if (existing.length) {
				const box = $('<div class="inv-existing">').appendTo(card);
				for (const conn of existing) renderExisting(box, spec, conn);
			}
		}

		function renderExisting(box, spec, conn) {
			const row = $('<div class="inv-conn-row">').appendTo(box);
			const left = $("<span>").appendTo(row);
			$('<span class="inv-conn-title">').text(conn.connection_name || conn.name).appendTo(left);
			$('<span class="inv-pill">')
				.addClass(`indicator-pill ${inv.statusIndicator(conn.status)}`)
				.addClass(inv.statusPillClass(conn.status))
				.text(conn.status || __("Not Connected"))
				.appendTo(left);
			if (!conn.enabled) {
				$('<span class="indicator-pill gray inv-pill inv-pill-gray">').text(__("Disabled")).appendTo(left);
			}
			const right = $("<span>").appendTo(row);
			$('<a class="btn btn-default btn-sm">')
				.attr("href", inv.formUrl("Broker Connection", conn.name))
				.text(__("Open"))
				.appendTo(right);

			if (spec.broker === "Zerodha" && inv.isManager()) {
				$('<button type="button" class="btn btn-default btn-sm">')
					.text(__("Connect with Zerodha"))
					.attr("aria-label", __("Connect with Zerodha") + ` ${conn.connection_name || conn.name}`)
					.on("click", () => zerodhaConnect(conn))
					.appendTo(right);
			}
		}

		/* Zerodha daily-token flow: open the Kite login in a new tab, then let
		 * the user paste the request_token from the redirect URL and exchange
		 * it server-side. Tokens expire daily — reconnect each trading day. */
		async function zerodhaConnect(conn) {
			if (state.exchanging[conn.name]) return;
			state.exchanging[conn.name] = true;
			try {
				const r = await call("zerodha_login_url", { connection: conn.name });
				if (window.open) window.open(r.url, "_blank", "noopener");
				showTokenDialog(conn);
			} catch (error) {
				frappe.show_alert({
					message: __("Could not start the Zerodha login. Check the API key on the connection."),
					indicator: "red",
				});
			} finally {
				state.exchanging[conn.name] = false;
			}
		}

		function showTokenDialog(conn) {
			const dialog = new frappe.ui.Dialog({
				title: __("Complete Zerodha Login"),
				fields: [
					{ fieldname: "help", fieldtype: "HTML",
						options: `<p class="text-muted">${inv.escape(
							__("Finish logging in on the Kite tab. Your browser is then redirected to a URL with a request_token=… parameter — paste that token here. Kite tokens expire daily, so reconnect each trading day.")
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
					await call("zerodha_exchange_token", { connection: conn.name, request_token: token });
					dialog.hide();
					frappe.show_alert({ message: __("Zerodha connected for today."), indicator: "green" });
					load();
				},
			});
			dialog.show();
		}

		load();
	};

	frappe.pages["broker-setup"].on_page_show = function (wrapper) {
		if (wrapper.broker_setup_reload && !wrapper.broker_setup_state.loading) {
			wrapper.broker_setup_reload();
		}
	};
})();
