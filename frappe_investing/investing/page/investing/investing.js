/* Frappe Investing — portfolio dashboard (page route: investing).
 *
 * Consumes the frozen API in frappe_investing/api.py (get_dashboard,
 * create_portfolio, record_manual_event, sync_now, refresh_prices,
 * benchmark_list, compare_benchmark). All rendering goes through jQuery
 * .text() so dynamic strings are never injected as HTML; the benchmark
 * chart uses frappe-charts (bundled with Desk, no extra dependency).
 */
(() => {
	const inv = (frappe.investing = frappe.investing || {});

	/* ------------------------------------------------ event field specs
	 * Visible/required fields per event type, mirroring the validation in
	 * frappe_investing/core/events.py. Exported on the namespace so the
	 * dialog and the tests share one source of truth.
	 */
	const EVENT_TYPES = [
		"Buy", "Sell", "Dividend", "Coupon", "Interest", "Fee", "Deposit",
		"Withdrawal", "Transfer In", "Transfer Out", "Split", "Reverse Split",
		"Stock Dividend", "Spin-off", "Cash-in-lieu", "Redemption", "FX Conversion",
	];

	// fieldname -> {reqd} for everything beyond the base fields
	// (event_type, account, posting_date, currency, notes).
	const TYPE_FIELDS = {
		"Buy": { security: 1, qty: 1, price: 1, fees: 0, taxes: 0 },
		"Sell": { security: 1, qty: 1, price: 1, fees: 0, taxes: 0 },
		"Dividend": { security: 1, gross: 1, taxes: 0 },
		"Coupon": { security: 1, gross: 1, taxes: 0 },
		"Interest": { gross: 1, taxes: 0 },
		"Fee": { amount: 1 },
		"Deposit": { amount: 1 },
		"Withdrawal": { amount: 1 },
		"Transfer In": { security: 1, qty: 1 },
		"Transfer Out": { security: 1, qty: 1, target_account: 1 },
		"Split": { security: 1, split_ratio: 1 },
		"Reverse Split": { security: 1, split_ratio: 1 },
		"Stock Dividend": { security: 1, split_ratio: 1 },
		"Spin-off": { security: 1, child_security: 1, basis_allocation: 1, child_ratio: 1 },
		"Cash-in-lieu": { security: 1, qty: 1, price: 1 },
		"Redemption": { security: 1, qty: 1, price: 1 },
		"FX Conversion": { amount: 1, target_currency: 1, target_amount: 1 },
	};

	const EXTRA_FIELDNAMES = [
		"security", "qty", "price", "amount", "gross", "fees", "taxes",
		"split_ratio", "child_security", "basis_allocation", "child_ratio",
		"target_currency", "target_amount", "target_account",
	];

	inv.eventTypes = EVENT_TYPES.slice();
	inv.eventFieldSpec = function (eventType) {
		return TYPE_FIELDS[eventType] || {};
	};

	/* ------------------------------------------------- event validation
	 * Client-side mirror of core/events.py validate(); returns an array of
	 * translated error strings (empty when valid).
	 */
	inv.validateEvent = function (values) {
		const errs = [];
		const type = values.event_type;
		const num = (v) => inv.num(v);
		const positive = (v) => num(v) !== null && num(v) > 0;
		const nonNeg = (v) => num(v) !== null && num(v) >= 0;
		const need = (ok, msg) => { if (!ok) errs.push(msg); };

		need(!!type, __("Choose an event type."));
		need(!!values.account, __("Choose an investment account."));
		need(!!values.posting_date, __("Choose a posting date."));
		need(!!values.currency, __("Choose a currency."));
		if (!type) return errs;

		const spec = TYPE_FIELDS[type] || {};
		if (spec.security) need(!!values.security, __("{0} requires a security.", [type]));
		if ("qty" in spec) {
			need(positive(values.qty), __("{0} requires a positive quantity.", [type]));
		}
		if ("price" in spec && spec.price) {
			need(nonNeg(values.price), __("{0} requires a non-negative price.", [type]));
		}
		if ("gross" in spec && spec.gross) {
			need(nonNeg(values.gross), __("{0} requires a non-negative gross amount.", [type]));
		}
		if ("amount" in spec && spec.amount) {
			need(positive(values.amount), __("{0} requires a positive amount.", [type]));
		}
		if (type === "Split") need(positive(values.split_ratio) && num(values.split_ratio) > 1,
			__("Split ratio must be above 1."));
		if (type === "Reverse Split") need(positive(values.split_ratio) && num(values.split_ratio) < 1,
			__("Reverse Split ratio must be below 1."));
		if (type === "Stock Dividend") need(positive(values.split_ratio),
			__("Stock Dividend requires a positive ratio."));
		if (type === "Spin-off") {
			need(!!values.child_security, __("Spin-off requires a child security."));
			const basis = num(values.basis_allocation);
			need(basis !== null && basis > 0 && basis < 1,
				__("Spin-off requires a basis allocation between 0 and 1."));
			need(positive(values.child_ratio), __("Spin-off requires a positive child ratio."));
		}
		if (type === "FX Conversion") {
			need(!!values.target_currency, __("FX Conversion requires a target currency."));
			need(positive(values.target_amount), __("FX Conversion requires a target amount."));
		}
		if (type === "Transfer Out") {
			need(!!values.target_account, __("Transfer Out requires a target account."));
			need(values.target_account !== values.account,
				__("Transfer Out target must differ from the source account."));
		}
		// Optional numeric fields must be numbers when present.
		for (const f of ["fees", "taxes"]) {
			if (values[f] !== undefined && values[f] !== null && values[f] !== "" && num(values[f]) === null) {
				errs.push(__("{0} must be a number.", [f]));
			}
		}
		return errs;
	};

	/* ------------------------------------------------------------ page */
	frappe.pages["investing"] = frappe.pages["investing"] || {};

	frappe.pages["investing"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Investing"),
			single_column: true,
		});
		const root = $('<div class="inv-root"></div>').appendTo(page.main);

		const state = {
			portfolio: null,
			data: null,
			sort: { key: "market_value", dir: -1 },
			syncing: {}, // connection name -> true while its sync_now call is in flight
			// syncPollTimer holds the interval id used to poll a connection's
			// status/last_sync after Sync Now is queued. It is always cleared
			// via stopSyncPoll(): on status change, on timeout, and on page
			// unload (router change below).
			syncPollTimer: null,
			pollTicks: 0,
			loading: null,
		};
		wrapper.investing_state = state;

		const stopSyncPoll = () => {
			if (state.syncPollTimer) {
				clearInterval(state.syncPollTimer);
				state.syncPollTimer = null;
			}
		};
		wrapper.investing_stop_poll = stopSyncPoll;
		try {
			if (frappe.router && typeof frappe.router.on === "function") {
				frappe.router.on("change", stopSyncPoll); // clear the interval on page unload
			}
		} catch (e) { /* older router without .on — poll still times out */ }

		const call = async (method, args = {}) =>
			(await frappe.call({ method: `frappe_investing.api.${method}`, args })).message;

		async function load() {
			if (state.loading) return state.loading;
			state.loading = (async () => {
				try {
					const data = await call("get_dashboard", { portfolio: state.portfolio });
					state.data = data;
					state.benchmarks = await call("benchmark_list");
					if (!data.needs_setup) state.portfolio = data.portfolio;
					render();
				} catch (error) {
					renderError(error);
				} finally {
					state.loading = null;
				}
			})();
			return state.loading;
		}
		wrapper.investing_reload = load;

		/* ----------------------------------------------------- renderers */
		function render() {
			root.empty();
			const data = state.data;
			if (!data) return;
			if (data.needs_setup) {
				renderEmpty(data);
				return;
			}
			renderToolbar(data);
			renderBenchmark();
			renderHoldings(data);
			renderStatementImport(data);
			renderConnections(data);
			renderPending(data);
			renderRecent(data);
		}

		/* ---------------------------------------- statement CSV import
		 * Two tabs, Desk-native (.form-tabs / .nav-tabs): tab 1 is the
		 * drag-and-drop statement importer (portfolio-aware via the account
		 * picker, with the cost-method selector surfaced before posting);
		 * tab 2 is the supported-operations reference (every event type with
		 * its required columns, served by import_reference so docs and the
		 * server validator cannot drift).
		 */
		function renderStatementImport(data) {
			const section = $('<section class="inv-section inv-import"></section>').appendTo(root);
			$("<h2>").text(__("Statement Import")).appendTo(section);
			$('<p class="inv-section-sub">')
				.text(__("Import a broker statement CSV into the picked account. Nothing posts until you review the preview."))
				.appendTo(section);
			const tabs = $('<div class="form-tabs">').appendTo(section);
			const nav = $('<ul class="nav nav-tabs" role="tablist">').appendTo(tabs);
			const panes = $('<div class="tab-content">').appendTo(tabs);
			const importTab = { id: "inv-import-tab", label: __("Import Statement") };
			const refTab = { id: "inv-reference-tab", label: __("Supported Operations") };
			[state.importTab, state.refTab] = [importTab.id, refTab.id];
			const navItems = [];
			for (const tab of [importTab, refTab]) {
				const li = $('<li role="presentation">').appendTo(nav);
				const link = $('<a role="tab">')
					.attr("href", `#${tab.id}`)
					.attr("aria-controls", tab.id)
					.text(tab.label)
					.on("click", (e) => {
						e.preventDefault();
						state.activeImportTab = tab.id;
						nav.find("li").removeClass("active");
						li.addClass("active");
						panes.find(".tab-pane").removeClass("active");
						panes.find(`#${tab.id}`).addClass("active");
					})
					.appendTo(li);
				navItems.push({ li, link });
				$('<div class="tab-pane" role="tabpanel">').attr("id", tab.id).appendTo(panes);
			}
			navItems[0].li.addClass("active");
			panes.find(`#${importTab.id}`).addClass("active");
			state.activeImportTab = state.activeImportTab || importTab.id;
			if (state.activeImportTab === refTab.id) navItems[1].link.trigger("click");
			renderImportPane(panes.find(`#${importTab.id}`), data);
			renderReferencePane(panes.find(`#${refTab.id}`));
		}

		function renderError(error) {
			root.empty();
			const box = $('<div class="inv-empty" role="alert"></div>').appendTo(root);
			$("<h2>").text(__("The dashboard could not be loaded")).appendTo(box);
			$("<p>").text(
				error && error.message && error.message !== "undefined"
					? String(error.message)
					: __("Check your permissions and try again.")
			).appendTo(box);
			$('<button type="button" class="btn btn-default">')
				.text(__("Retry"))
				.on("click", () => load())
				.appendTo(box);
		}

		function renderEmpty(data) {
			const box = $('<div class="inv-empty"></div>').appendTo(root);
			$("<h2>").text(__("Set up your first portfolio")).appendTo(box);
			$("<p>").text(
				__("Create a portfolio, connect a broker or import a CSV, and your holdings, performance and accounting status will appear here.")
			).appendTo(box);
			const actions = $('<div class="inv-empty-actions">').appendTo(box);
			if (inv.isManager()) {
				$('<button type="button" class="btn btn-primary">')
					.text(__("Create Portfolio"))
					.on("click", () => showPortfolioDialog())
					.appendTo(actions);
			} else {
				$('<p class="inv-muted">')
					.text(__("Ask an Investment Manager to create a portfolio for you."))
					.appendTo(actions);
			}
			$('<button type="button" class="btn btn-default">')
				.text(__("Open Broker Setup"))
				.on("click", () => frappe.set_route("broker-setup"))
				.appendTo(actions);
		}

		function renderToolbar(data) {
			const bar = $('<div class="inv-toolbar"></div>').appendTo(root);
			const picker = $('<div class="inv-portfolio-picker">').appendTo(bar);
			const selectId = "inv-portfolio-select";
			$("<label>").attr("for", selectId).text(__("Portfolio")).appendTo(picker);
			const select = $('<select class="form-control">')
				.attr("id", selectId)
				.attr("aria-label", __("Portfolio"))
				.appendTo(picker);
			for (const p of data.portfolios || []) {
				$("<option>")
					.attr("value", p.name)
					.text(p.portfolio_name || p.name)
					.appendTo(select);
			}
			select.val(state.portfolio);
			select.on("change", () => {
				state.portfolio = select.val();
				load();
			});
			const actions = $('<div class="inv-actions">').appendTo(bar);
			if (inv.canRecord()) {
				$('<button type="button" class="btn btn-primary">')
					.text(__("Record Event"))
					.on("click", () => showEventDialog())
					.appendTo(actions);
			}
			if (inv.isManager()) {
				$('<button type="button" class="btn btn-default">')
					.text(__("Refresh Prices"))
					.on("click", () => refreshPrices())
					.appendTo(actions);
			}
		}

		function renderBenchmark() {
			const section = $('<section class="inv-section"></section>').appendTo(root);
			$("<h2>").text(__("Compare to Benchmark")).appendTo(section);
			const controls = $('<div class="inv-benchmark-controls">').appendTo(section);
			const select = $('<select class="form-control">')
				.attr("aria-label", __("Benchmark"))
				.appendTo(controls);
			$("<option>").attr("value", "").text(__("Choose a benchmark…")).appendTo(select);
			state.benchmarks = state.benchmarks || [];
			for (const b of state.benchmarks) {
				$("<option>").attr("value", b.code).text(b.name).appendTo(select);
			}
			if (state.benchmark) select.val(state.benchmark);
			const out = $('<div class="inv-benchmark-result">').appendTo(section);
			if (!state.benchmark) {
				$('<p class="inv-muted">')
					.text(__("Pick an index to compare this portfolio's YTD return against it."))
					.appendTo(out);
			}
			select.on("change", async () => {
				state.benchmark = select.val() || null;
				out.empty();
				if (!state.benchmark) {
					$('<p class="inv-muted">')
						.text(__("Pick an index to compare this portfolio's YTD return against it."))
						.appendTo(out);
					return;
				}
				$('<p class="inv-muted">').text(__("Comparing…")).appendTo(out);
				try {
					const r = await call("compare_benchmark", {
						portfolio: state.portfolio,
						benchmark: state.benchmark,
					});
					renderBenchmarkResult(out, r);
				} catch (error) {
					out.empty();
					$('<p class="inv-muted">').text(__("The comparison could not be computed.")).appendTo(out);
				}
			});
		}

		/* Portfolio vs benchmark goes through frappe-charts (bundled with
		 * Desk as window.frappe.Chart): a two-series line built by the
		 * shared inv.benchmarkChartData helper. Numbers come only from the
		 * API: null stays missing and the note carries the honest reason. */
		function renderBenchmarkResult(out, r) {
			out.empty();
			if (r.benchmark_return_ytd === null || r.benchmark_return_ytd === undefined) {
				$('<p class="inv-muted">').text(r.note || __("No benchmark data for this period.")).appendTo(out);
				return;
			}
			const mount = $('<div class="inv-benchmark-chart">').appendTo(out);
			// frappe-charts is bundled with Desk as window.frappe.Chart; the
			// window.Chart fallback keeps the harness and any non-Desk
			// embedding honest about which constructor was used.
			const ChartCtor = window.Chart || (window.frappe && window.frappe.Chart);
			if (typeof ChartCtor !== "function") {
				mount.text(__("Charts are unavailable in this Desk build."));
				return;
			}
			try {
				new ChartCtor(mount.get(0), {
					title: __("Portfolio vs {0} (YTD %)", [r.benchmark_name || r.benchmark]),
					type: "line",
					height: 220,
					axisOptions: { xIsSeries: true },
					tooltipOptions: { formatTooltipY: (d) => `${d}%` },
					data: inv.benchmarkChartData(r),
				});
			} catch (error) {
				mount.text(__("The comparison could not be rendered."));
			}
			if (r.excess_return_ytd !== null && r.excess_return_ytd !== undefined) {
				$('<p class="inv-muted">')
					.text(__("Excess vs benchmark: {0}", [inv.formatPercent(r.excess_return_ytd)]))
					.appendTo(out);
			}
			if (r.note) $('<p class="inv-muted">').text(r.note).appendTo(out);
			if (r.benchmark_currency && r.base_currency && r.benchmark_currency !== r.base_currency) {
				$('<p class="inv-section-sub">')
					.text(__("Benchmark converted from {0} to {1} at your FX rates.", [r.benchmark_currency, r.base_currency]))
					.appendTo(out);
			}
		}

		function holdingRows(data) {
			const values = data.values || {};
			const stale = new Set(values.stale || []);
			return Object.entries(values.by_security || {}).map(([security, bucket]) => {
				const qty = inv.num(bucket.qty);
				const mv = inv.num(bucket.market_value);
				const lastPrice = inv.num(bucket.last_price);
				return {
					security,
					qty,
					market_value: mv,
					cost: inv.num(bucket.cost),
					unrealized_pnl: inv.num(bucket.unrealized_pnl),
					asset_class: bucket.asset_class,
					stale: stale.has(security) || mv === null,
					// Prefer the actual last price in the security's own currency;
					// fall back to the implied price (market value / qty, base).
					price: lastPrice !== null ? lastPrice : mv !== null && qty ? mv / qty : null,
					price_currency: lastPrice !== null ? bucket.price_currency || values.base : values.base,
				};
			});
		}

		function sortHoldings(rows) {
			const { key, dir } = state.sort;
			const val = (row) => (row[key] === null || row[key] === undefined ? null : row[key]);
			return rows.slice().sort((a, b) => {
				const va = val(a);
				const vb = val(b);
				if (va === null && vb === null) return String(a.security).localeCompare(String(b.security));
				if (va === null) return 1; // stale/unpriced always sink
				if (vb === null) return -1;
				if (typeof va === "string") return dir * va.localeCompare(String(vb));
				return dir * (va - vb);
			});
		}

		function renderHoldings(data) {
			const values = data.values || {};
			const rows = sortHoldings(holdingRows(data));
			const section = $('<section class="inv-section"></section>').appendTo(root);
			$("<h2>").text(__("Holdings")).appendTo(section);
			$('<p class="inv-section-sub">')
				.text(__("Prices and values in {0}, as of {1}.", [values.base || "", inv.formatDate(values.day)]))
				.appendTo(section);
			if (!rows.length) {
				$('<p class="inv-muted">').text(__("No holdings yet. Record an event or sync a broker.")).appendTo(section);
				return;
			}
			const wrap = $('<div class="inv-table-wrap">').appendTo(section);
			const table = $('<table class="inv-table">').appendTo(wrap);
			const thead = $("<thead>").appendTo(table);
			const tr = $("<tr>").appendTo(thead);
			const columns = [
				{ key: "security", label: __("Security"), num: false },
				{ key: "qty", label: __("Qty"), num: true },
				{ key: "price", label: __("Price"), num: true },
				{ key: "market_value", label: __("Market Value"), num: true },
				{ key: "cost", label: __("Cost"), num: true },
				{ key: "unrealized_pnl", label: __("Unrealized P&L"), num: true },
			];
			for (const col of columns) {
				const th = $("<th>").attr("scope", "col").appendTo(tr);
				if (col.num) th.addClass("inv-num");
				const active = state.sort.key === col.key;
				th.attr("aria-sort", active ? (state.sort.dir === 1 ? "ascending" : "descending") : "none");
				const btn = $('<button type="button" class="inv-sort-btn">')
					.text(col.label)
					.on("click", () => {
						if (state.sort.key === col.key) state.sort.dir *= -1;
						else state.sort = { key: col.key, dir: col.key === "security" ? 1 : -1 };
						render();
					})
					.appendTo(th);
				if (active) {
					$('<span class="inv-sort-arrow" aria-hidden="true">')
						.text(state.sort.dir === 1 ? "▲" : "▼")
						.appendTo(btn);
				}
			}
			const tbody = $("<tbody>").appendTo(table);
			for (const row of rows) {
				const tr2 = $("<tr>").appendTo(tbody);
				const secTd = $("<td>").appendTo(tr2);
				$("<a>")
					.attr("href", inv.formUrl("Security", row.security))
					.text(row.security)
					.appendTo(secTd);
				$("<td>").addClass("inv-num").text(row.qty === null ? "—" : inv.formatQty(row.qty)).appendTo(tr2);
				const priceTd = $("<td>").addClass("inv-num").appendTo(tr2);
				if (row.stale) {
					$('<span class="indicator-pill yellow inv-badge-stale">').text(__("Stale")).appendTo(priceTd);
				} else {
					priceTd.text(row.price === null ? "—" : inv.formatMoney(row.price, row.price_currency || values.base));
				}
				$("<td>").addClass("inv-num").text(row.market_value === null ? "—" : inv.formatMoney(row.market_value, values.base)).appendTo(tr2);
				$("<td>").addClass("inv-num").text(row.cost === null ? "—" : inv.formatMoney(row.cost, values.base)).appendTo(tr2);
				$("<td>")
					.addClass("inv-num")
					.addClass(inv.signClass(row.unrealized_pnl))
					.text(row.unrealized_pnl === null ? "—" : inv.formatMoney(row.unrealized_pnl, values.base))
					.appendTo(tr2);
			}
		}

		function renderConnections(data) {
			const connections = data.connections || [];
			const section = $('<section class="inv-section"></section>').appendTo(root);
			$("<h2>").text(__("Broker Connections")).appendTo(section);
			if (!connections.length) {
				$('<p class="inv-muted">').text(__("No broker connections for this company.")).appendTo(section);
				$('<button type="button" class="btn btn-default">')
					.text(__("Open Broker Setup"))
					.on("click", () => frappe.set_route("broker-setup"))
					.appendTo(section);
				return;
			}
			for (const conn of connections) {
				const row = $('<div class="inv-conn">').appendTo(section);
				const main = $("<div>").appendTo(row);
				$('<span class="inv-conn-name">').text(conn.connection_name || conn.name).appendTo(main);
				$('<span class="inv-conn-meta">')
					.text(` ${conn.broker || ""} · ${__("Last sync")}: ${inv.formatDatetime(conn.last_sync)}`)
					.appendTo(main);
				const side = $('<div class="inv-conn-side">').appendTo(row);
				$('<span class="inv-pill">')
					.addClass(`indicator-pill ${inv.statusIndicator(conn.status)}`)
					.addClass(inv.statusPillClass(conn.status))
					.text(conn.status || __("Not Connected"))
					.appendTo(side);
				if (!conn.enabled) {
					$('<span class="indicator-pill gray inv-pill inv-pill-gray">').text(__("Disabled")).appendTo(side);
				}
				if (inv.isManager()) {
					const btn = $('<button type="button" class="btn btn-default btn-sm">')
						.text(__("Sync Now"))
						.prop("disabled", !!state.syncing[conn.name])
						.attr("aria-label", __("Sync Now") + ` ${conn.connection_name || conn.name}`)
						.on("click", () => syncNow(conn, btn))
						.appendTo(side);
				}
				if (conn.last_error && conn.status === "Error") {
					$('<div class="inv-conn-error">').text(conn.last_error).appendTo(row);
				}
			}
		}

		async function syncNow(conn, btn) {
			if (state.syncing[conn.name]) return; // in flight — ignore extra clicks
			state.syncing[conn.name] = true;
			if (btn && btn.prop) btn.prop("disabled", true);
			try {
				const r = await call("sync_now", { connection: conn.name });
				if (r && r.queued === false) {
					frappe.show_alert({
						message: r.note || __("A sync for {0} is already running.", [
							inv.escape(conn.connection_name || conn.name),
						]),
						indicator: "orange",
					});
					startSyncPoll(conn);
					return;
				}
				frappe.show_alert({
					message: __("Sync queued for {0}; it runs in the background.", [
						inv.escape(conn.connection_name || conn.name),
					]),
					indicator: "green",
				});
				startSyncPoll(conn);
			} catch (error) {
				frappe.show_alert({ message: __("Sync could not be queued."), indicator: "red" });
			} finally {
				state.syncing[conn.name] = false;
				if (btn && btn.prop) btn.prop("disabled", false);
			}
		}

		/* Poll a queued sync: watch Broker Connection status/last_sync until it
		 * changes, then reload the dashboard. state.syncPollTimer is the
		 * interval id (see declaration above); stopSyncPoll() clears it on
		 * change, timeout or page unload. */
		function startSyncPoll(conn) {
			stopSyncPoll();
			const baseline = { status: conn.status, last_sync: conn.last_sync };
			state.pollTicks = 0;
			state.syncPollTimer = setInterval(async () => {
				state.pollTicks += 1;
				if (state.pollTicks > 20) { // ~60s at 3s intervals
					stopSyncPoll();
					return;
				}
				try {
					const r = await frappe.call({
						method: "frappe.client.get_value",
						args: {
							doctype: "Broker Connection",
							filters: { name: conn.name },
							fieldname: JSON.stringify(["status", "last_sync"]),
						},
					});
					const current = (r && r.message) || {};
					if (current.status !== baseline.status || current.last_sync !== baseline.last_sync) {
						stopSyncPoll();
						frappe.show_alert({
							message: current.status === "Connected"
								? __("Sync finished for {0}.", [inv.escape(conn.connection_name || conn.name)])
								: __("Sync for {0} ended with status: {1}.", [
									inv.escape(conn.connection_name || conn.name),
									inv.escape(current.status || ""),
								]),
							indicator: current.status === "Connected" ? "green" : "orange",
						});
						load();
					}
				} catch (e) { /* transient read failure — keep polling until timeout */ }
			}, 3000);
		}

		function renderPending(data) {
			const pending = data.pending_accounting || [];
			const section = $('<section class="inv-section"></section>').appendTo(root);
			$("<h2>").text(__("Pending Accounting")).appendTo(section);
			if (!pending.length) {
				$('<p class="inv-muted">').text(__("No accounting exceptions. Every submitted event has posted or been skipped.")).appendTo(section);
				return;
			}
			const list = $('<ul class="inv-list-plain">').appendTo(section);
			for (const row of pending) {
				const li = $("<li>").appendTo(list);
				const left = $("<span>").appendTo(li);
				$("<a>")
					.attr("href", inv.formUrl("Investment Event", row.name))
					.text(`${row.event_type} · ${row.name}`)
					.appendTo(left);
				$('<span class="inv-muted">')
					.text(`${inv.formatDate(row.posting_date)}${row.security ? " · " + row.security : ""}`)
					.appendTo(li);
			}
		}

		function renderRecent(data) {
			const events = data.recent_events || [];
			const section = $('<section class="inv-section"></section>').appendTo(root);
			$("<h2>").text(__("Recent Events")).appendTo(section);
			if (!events.length) {
				$('<p class="inv-muted">').text(__("No events recorded yet.")).appendTo(section);
				return;
			}
			const wrap = $('<div class="inv-table-wrap">').appendTo(section);
			const table = $('<table class="inv-table">').appendTo(wrap);
			const thead = $("<thead>").appendTo(table);
			const tr = $("<tr>").appendTo(thead);
			for (const label of [__("Type"), __("Date"), __("Security"), __("Qty @ Price"), __("Accounting")]) {
				$("<th>").attr("scope", "col").text(label).appendTo(tr);
			}
			const tbody = $("<tbody>").appendTo(table);
			for (const event of events) {
				const row = $("<tr>").appendTo(tbody);
				const typeTd = $("<td>").appendTo(row);
				$("<a>")
					.attr("href", inv.formUrl("Investment Event", event.name))
					.text(event.event_type)
					.appendTo(typeTd);
				$("<td>").text(inv.formatDate(event.posting_date)).appendTo(row);
				const secTd = $("<td>").appendTo(row);
				if (event.security) {
					$("<a>")
						.attr("href", inv.formUrl("Security", event.security))
						.text(event.security)
						.appendTo(secTd);
				} else {
					secTd.text("—");
				}
				const qty = inv.num(event.qty);
				const price = inv.num(event.price);
				$("<td>")
					.addClass("inv-num")
					.text(qty === null ? "—" : `${inv.formatQty(qty)} @ ${price === null ? "—" : inv.formatMoney(price, event.currency)}`)
					.appendTo(row);
				const statusTd = $("<td>").appendTo(row);
				$('<span class="inv-pill">')
					.addClass(`indicator-pill ${inv.statusIndicator(event.accounting_status)}`)
					.addClass(inv.statusPillClass(event.accounting_status))
					.text(event.accounting_status || __("Pending"))
					.appendTo(statusTd);
			}
		}


		function renderImportPane(pane, data) {
			pane.empty();
			const form = $('<div class="inv-import-form">').appendTo(pane);

			// Target account: the import posts into the picked account, like
			// manual events do. Defaults to this portfolio's first account.
			const accountRow = $('<div class="inv-import-row">').appendTo(form);
			$("<label>").attr("for", "inv-import-account").text(__("Investment Account")).appendTo(accountRow);
			const accountSelect = $('<select class="form-control">')
				.attr("id", "inv-import-account")
				.attr("aria-label", __("Investment Account"))
				.appendTo(accountRow);
			// Accounts ride along on the dashboard payload (get_dashboard),
			// filtered here to this portfolio like the manual-event dialog.
			const accounts = (data.accounts || []).filter((a) => a.portfolio === state.portfolio);
			for (const a of accounts) {
				$("<option>").attr("value", a.name).text(a.account_name || a.name).appendTo(accountSelect);
			}
			if (state.importAccount) accountSelect.val(state.importAccount);
			if (!accountSelect.val() && accounts.length) {
				accountSelect.val(accounts[0].name); // default to the first account
			}
			accountSelect.on("change", () => {
				state.importAccount = accountSelect.val() || null;
				state.importPreview = null;
				render();
			});

			// Cost-method surface: the selector documents which method will
			// price the gains; the engine reads the portfolio setting.
			const methodRow = $('<div class="inv-import-row">').appendTo(form);
			$("<label>").attr("for", "inv-import-method").text(__("Cost Method")).appendTo(methodRow);
			const methodSelect = $('<select class="form-control">')
				.attr("id", "inv-import-method")
				.attr("aria-label", __("Cost Method"))
				.appendTo(methodRow);
			for (const m of ["FIFO", "LIFO", "AVERAGE", "SPECIFIC"]) {
				$("<option>").attr("value", m).text(m).appendTo(methodSelect);
			}
			const methodNote = $('<p class="inv-muted inv-import-method-note">').appendTo(methodRow);
			const refreshMethod = async () => {
				const account = accountSelect.val();
				if (!account) {
					methodNote.text(__("Pick an account to see its cost method."));
					return;
				}
				try {
					const info = await call("import_cost_method", { account });
					methodSelect.val(info.effective || "FIFO");
					methodNote.text(
						info.portfolio_cost_method
							? __("Gains for {0} use {1} (set on the portfolio).", [info.portfolio, info.effective])
							: __("Gains use the default {0}. Set a method on the portfolio to override.", [info.effective || "FIFO"])
					);
				} catch (e) {
					methodNote.text(__("The cost method could not be loaded."));
				}
			};
			methodSelect.on("change", () => {
				methodNote.text(__("The cost method lives on the portfolio — change it on the Portfolio form; gains use it on posting."));
			});
			refreshMethod();

			// Template download (headers + one example row, served from the app).
			const templateRow = $('<div class="inv-import-row">').appendTo(form);
			$('<button type="button" class="btn btn-default btn-sm">')
				.text(__("Download CSV Template"))
				.on("click", async () => {
					try {
						const r = await call("import_template_url", {});
						if (window.open) window.open(r.url, "_blank");
					} catch (e) {
						frappe.show_alert({ message: __("The template could not be loaded."), indicator: "red" });
					}
				})
				.appendTo(templateRow);

			// Drag-and-drop zone with a file-picker fallback. Files are read
			// locally with FileReader; only the text is sent to the server.
			const drop = $('<div class="inv-dropzone" tabindex="0" role="button">')
				.attr("aria-label", __("Drop a statement CSV here, or choose a file"))
				.appendTo(form);
			$("<p>").text(__("Drop a statement CSV here, or choose a file.")).appendTo(drop);
			const fileInput = $('<input type="file" accept=".csv,text/csv">')
				.attr("aria-label", __("Choose a statement CSV file"))
				.appendTo(drop);
			const hint = state.importFileName
				? __("Selected: {0}", [state.importFileName])
				: __("Nothing selected yet.");
			$('<p class="inv-muted inv-import-file">').text(hint).appendTo(drop);
			const readFile = (file) => {
				if (!file) return;
				state.importFileName = file.name || "";
				// window.FileReader in Desk; injectable in tests via the harness window.
				const Ctor = window.FileReader || (typeof FileReader !== "undefined" ? FileReader : null);
				if (!Ctor) {
					frappe.show_alert({ message: __("File reading is unavailable here."), indicator: "red" });
					return;
				}
				const reader = new Ctor();
				reader.onload = () => {
					state.importText = String(reader.result || "");
					state.importPreview = null;
					render();
				};
				reader.onerror = () => {
					frappe.show_alert({ message: __("The file could not be read."), indicator: "red" });
				};
				reader.readAsText(file);
			};
			fileInput.on("change", (e) => {
				const files = (e && e.target && e.target.files) || (fileInput.get(0) && fileInput.get(0).files);
				if (files && files[0]) readFile(files[0]);
			});
			drop.on("dragover", (e) => { e.preventDefault(); drop.addClass("inv-dropzone-active"); });
			drop.on("dragleave", () => drop.removeClass("inv-dropzone-active"));
			drop.on("drop", (e) => {
				e.preventDefault();
				drop.removeClass("inv-dropzone-active");
				const dt = e.originalEvent && e.originalEvent.dataTransfer;
				if (dt && dt.files && dt.files[0]) readFile(dt.files[0]);
			});

			if (state.importText) {
				renderImportPreview(pane, accountSelect.val());
			}
		}

		// Preview counts + per-row errors first; the Post button appears only
		// when the preview is clean. Posting routes through import_post, hence
		// the same record_event dedupe as manual events.
		async function renderImportPreview(pane, account) {
			const box = $('<div class="inv-import-preview">').appendTo(pane);
			$('<p class="inv-muted">').text(__("Checking…")).appendTo(box);
			let preview = state.importPreview;
			if (!preview) {
				try {
					preview = await call("import_preview", { account, csv_text: state.importText });
					state.importPreview = preview;
				} catch (e) {
					box.empty();
					$('<p class="inv-muted">')
						.text((e && e.message) || __("The file could not be checked."))
						.appendTo(box);
					return;
				}
			}
			box.empty();
			const summary = $('<p class="inv-import-summary">')
				.text(__("{0} rows: {1} valid, {2} with errors.", [preview.total_rows, preview.valid_rows, preview.error_rows]))
				.appendTo(box);
			if (preview.by_type && Object.keys(preview.by_type).length) {
				$('<p class="inv-muted">')
					.text(Object.entries(preview.by_type).map(([t, n]) => `${t}: ${n}`).join(" · "))
					.appendTo(box);
			}
			if ((preview.errors || []).length) {
				const wrap = $('<div class="inv-table-wrap">').appendTo(box);
				const table = $('<table class="inv-table">').appendTo(wrap);
				const thead = $("<thead>").appendTo(table);
				const tr = $("<tr>").appendTo(thead);
				$("<th>").attr("scope", "col").text(__("Row")).appendTo(tr);
				$("<th>").attr("scope", "col").text(__("Error")).appendTo(tr);
				const tbody = $("<tbody>").appendTo(table);
				for (const err of preview.errors) {
					const row = $("<tr>").appendTo(tbody);
					$("<td>").addClass("inv-num").text(err.row).appendTo(row);
					$("<td>").text(err.message).appendTo(row);
				}
				if (preview.batch) {
					$('<p class="inv-muted">')
						.text(__("Saved as Import Batch {0} — fix the file and check again.", [preview.batch]))
						.appendTo(box);
				}
				return;
			}
			$('<button type="button" class="btn btn-primary">')
				.text(__("Post {0} Events", [preview.valid_rows]))
				.on("click", async (e) => {
					const btn = $(e.currentTarget || e.target);
					if (btn.prop) btn.prop("disabled", true);
					try {
						const r = await call("import_post", { account, csv_text: state.importText });
						state.importText = null;
						state.importFileName = "";
						state.importPreview = null;
						frappe.show_alert({
							message: r.created === r.posted
								? __("Posted {0} events.", [r.posted])
								: __("Posted {0} events ({1} already existed).", [r.posted, r.posted - r.created]),
							indicator: "green",
						});
						load();
					} catch (err) {
						frappe.show_alert({
							message: (err && err.message) || __("Posting failed."),
							indicator: "red",
						});
						if (btn.prop) btn.prop("disabled", false);
					}
				})
				.appendTo(box);
		}

		function renderReferencePane(pane) {
			pane.empty();
			$('<p class="inv-section-sub">')
				.text(__("Every row needs date, type and currency, plus the columns below."))
				.appendTo(pane);
			const box = $('<div class="inv-reference">').appendTo(pane);
			$('<p class="inv-muted">').text(__("Loading…")).appendTo(box);
			call("import_reference", {}).then((r) => {
				box.empty();
				const wrap = $('<div class="inv-table-wrap">').appendTo(box);
				const table = $('<table class="inv-table">').appendTo(wrap);
				const thead = $("<thead>").appendTo(table);
				const tr = $("<tr>").appendTo(thead);
				for (const label of [__("Operation"), __("Required Columns"), __("Optional Columns")]) {
					$("<th>").attr("scope", "col").text(label).appendTo(tr);
				}
				const tbody = $("<tbody>").appendTo(table);
				for (const row of (r && r.event_types) || []) {
					const line = $("<tr>").appendTo(tbody);
					$("<td>").text(row.type).appendTo(line);
					$("<td>").text((row.required || []).join(", ")).appendTo(line);
					$("<td>").text((row.optional || []).join(", ")).appendTo(line);
				}
			}).catch(() => {
				box.empty();
				$('<p class="inv-muted">').text(__("The reference could not be loaded.")).appendTo(box);
			});
		}

		/* ------------------------------------------------------- dialogs */
		function showPortfolioDialog() {
			const dialog = new frappe.ui.Dialog({
				title: __("Create Portfolio"),
				fields: [
					{ fieldname: "portfolio_name", label: __("Portfolio Name"), fieldtype: "Data", reqd: 1,
						description: __("1–140 characters.") },
					{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company", reqd: 1,
						default: frappe.defaults && frappe.defaults.get_user_default
							? frappe.defaults.get_user_default("Company") : null },
					{ fieldname: "base_currency", label: __("Base Currency"), fieldtype: "Link", options: "Currency" },
				],
				primary_action_label: __("Create"),
				primary_action: async (values) => {
					const name = (values.portfolio_name || "").trim();
					if (!name || name.length > 140) {
						frappe.throw(__("Portfolio name must be 1–140 characters."));
						return;
					}
					if (!values.company) {
						frappe.throw(__("Choose a company."));
						return;
					}
					const r = await call("create_portfolio", {
						portfolio_name: name,
						company: values.company,
						base_currency: values.base_currency || null,
					});
					dialog.hide();
					state.portfolio = r.name;
					frappe.show_alert({ message: __("Portfolio created."), indicator: "green" });
					load();
				},
			});
			dialog.show();
		}


		function showEventDialog() {
			const data = state.data || {};
			const base = data.values && data.values.base;
			const fields = [
				{ fieldname: "event_type", label: __("Event Type"), fieldtype: "Select",
					options: EVENT_TYPES.join("\n"), reqd: 1, default: "Buy",
					onchange: () => applyEventType(dialog) },
				{ fieldname: "account", label: __("Investment Account"), fieldtype: "Link",
					options: "Investment Account", reqd: 1,
					get_query: () => ({ filters: { portfolio: state.portfolio, enabled: 1 } }) },
				{ fieldname: "posting_date", label: __("Posting Date"), fieldtype: "Date", reqd: 1,
					default: frappe.datetime && frappe.datetime.get_today ? frappe.datetime.get_today() : null },
				{ fieldname: "currency", label: __("Currency"), fieldtype: "Link", options: "Currency",
					reqd: 1, default: base || null },
				{ fieldname: "security", label: __("Security"), fieldtype: "Link", options: "Security" },
				{ fieldname: "qty", label: __("Quantity"), fieldtype: "Float" },
				{ fieldname: "price", label: __("Price per Unit"), fieldtype: "Float" },
				{ fieldname: "amount", label: __("Amount"), fieldtype: "Float" },
				{ fieldname: "gross", label: __("Gross Amount"), fieldtype: "Float" },
				{ fieldname: "fees", label: __("Fees"), fieldtype: "Float" },
				{ fieldname: "taxes", label: __("Taxes / Withholding"), fieldtype: "Float" },
				{ fieldname: "split_ratio", label: __("Split Ratio"), fieldtype: "Float",
					description: __("New shares per old share. Split 4 means 4-for-1; a reverse split uses a ratio below 1.") },
				{ fieldname: "child_security", label: __("Child Security"), fieldtype: "Link", options: "Security" },
				{ fieldname: "basis_allocation", label: __("Basis Allocation"), fieldtype: "Float",
					description: __("Fraction of the parent cost basis moving to the child, between 0 and 1.") },
				{ fieldname: "child_ratio", label: __("Child Ratio"), fieldtype: "Float",
					description: __("Child shares received per parent share.") },
				{ fieldname: "target_currency", label: __("Target Currency"), fieldtype: "Link", options: "Currency" },
				{ fieldname: "target_amount", label: __("Target Amount"), fieldtype: "Float" },
				{ fieldname: "target_account", label: __("Target Account"), fieldtype: "Link", options: "Investment Account",
					description: __("Transfer Out destination. Must be in the same portfolio.") },
				{ fieldname: "notes", label: __("Notes"), fieldtype: "Small Text" },
			];
			const dialog = new frappe.ui.Dialog({
				title: __("Record Investment Event"),
				fields,
				primary_action_label: __("Record"),
				primary_action: async (values) => {
					const errs = inv.validateEvent(values || {});
					if (errs.length) {
						frappe.throw(errs.join("\n"));
						return;
					}
					const r = await call("record_manual_event", values);
					dialog.hide();
					frappe.show_alert({
						message: r.created
							? __("Event {0} recorded.", [inv.escape(r.name)])
							: __("This event was already recorded as {0}.", [inv.escape(r.name)]),
						indicator: "green",
					});
					load();
				},
			});
			applyEventType(dialog);
			dialog.show();
		}

		// Toggle visibility/requirement of the extra fields for the chosen type.
		function applyEventType(dialog) {
			const type = dialog.get_value("event_type") || "Buy";
			const spec = TYPE_FIELDS[type] || {};
			for (const fieldname of EXTRA_FIELDNAMES) {
				const visible = fieldname in spec;
				dialog.set_df_property(fieldname, "hidden", visible ? 0 : 1);
				dialog.set_df_property(fieldname, "reqd", visible && spec[fieldname] ? 1 : 0);
			}
		}

		async function refreshPrices() {
			try {
				const r = await call("refresh_prices", {});
				frappe.show_alert({
					message: r.note ? String(r.note) : __("{0} prices updated.", [String(r.updated || 0)]),
					indicator: "green",
				});
				load();
			} catch (error) {
				frappe.show_alert({ message: __("Price refresh failed."), indicator: "red" });
			}
		}

		load();
	};

	frappe.pages["investing"].on_page_show = function (wrapper) {
		// on_page_load already kicked off the first load; reload only on
		// subsequent visits to the cached page.
		if (wrapper.investing_reload && !wrapper.investing_state.loading) {
			wrapper.investing_reload();
		}
	};
})();
