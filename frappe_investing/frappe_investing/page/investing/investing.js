/* Frappe Investing — portfolio dashboard (page route: investing).
 *
 * Consumes the frozen API in frappe_investing/api.py (get_dashboard,
 * create_portfolio, record_manual_event, save_license, sync_now,
 * refresh_prices). All rendering goes through jQuery .text() so dynamic
 * strings are never injected as HTML; signed values are colour-coded purely
 * through CSS classes.
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
		"Transfer Out": { security: 1, qty: 1 },
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
		"target_currency", "target_amount",
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
				renderLicense(data, root);
				return;
			}
			renderToolbar(data);
			renderKpis(data);
			renderUsage(data);
			renderBenchmark(data);
			renderAllocation(data);
			renderHoldings(data);
			renderConnections(data);
			renderPending(data);
			renderRecent(data);
			renderLicense(data, root);
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

		function kpiCard(parent, label, valueText, valueClass, note) {
			const card = $('<div class="inv-kpi">').appendTo(parent);
			$('<div class="inv-kpi-label">').text(label).appendTo(card);
			$('<div class="inv-kpi-value">').addClass(valueClass || "").text(valueText).appendTo(card);
			if (note) $('<div class="inv-kpi-note">').text(note).appendTo(card);
			return card;
		}

		function renderKpis(data) {
			const values = data.values || {};
			const perf = data.performance || {};
			const base = values.base;
			const grid = $('<div class="inv-kpis">').appendTo(root);
			kpiCard(grid, __("Portfolio Value"), inv.formatMoney(values.total_value, base));
			kpiCard(grid, __("YTD TWR"), inv.formatPercent(perf.twr_ytd), inv.signClass(perf.twr_ytd),
				perf.snapshots < 2 ? __("Needs two daily snapshots") : null);
			kpiCard(grid, __("YTD XIRR"),
				perf.xirr_ytd === null || perf.xirr_ytd === undefined ? "—" : inv.formatPercent(perf.xirr_ytd),
				inv.signClass(perf.xirr_ytd));
			// Sharpe needs >=2 daily returns with variance; a flat or one-day
			// series shows a dash, not a misleading zero.
			kpiCard(grid, __("Sharpe (YTD)"),
				perf.sharpe_ytd === null || perf.sharpe_ytd === undefined ? "—" : inv.num(perf.sharpe_ytd).toFixed(2),
				inv.signClass(perf.sharpe_ytd),
				perf.sharpe_ytd === null || perf.sharpe_ytd === undefined ? __("Needs varied daily snapshots") : null);
			kpiCard(grid, __("Unrealized P&L"), inv.formatMoney(values.unrealized_pnl, base),
				inv.signClass(values.unrealized_pnl));
			// YTD realized P&L and income come from services.performance_summary
			// (_period_totals); a dash only appears if an older server omits them.
			const realized = perf.realized_pnl_ytd;
			const income = perf.income_ytd;
			kpiCard(grid, __("Realized P&L (YTD)"),
				realized === null || realized === undefined ? "—" : inv.formatMoney(realized, base),
				inv.signClass(realized));
			kpiCard(grid, __("Income (YTD)"),
				income === null || income === undefined ? "—" : inv.formatMoney(income, base),
				inv.signClass(income));
		}

		function renderBenchmark(data) {
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
			if (inv.isManager()) {
				$('<button type="button" class="btn btn-default btn-sm">')
					.text(__("Refresh Prices"))
					.on("click", async (e) => {
						const btn = $(e.currentTarget).prop("disabled", true);
						try {
							const r = await call("refresh_benchmark_prices");
							const updated = Object.keys(r || {}).filter((k) => r[k] !== null).length;
							frappe.show_alert({
								message: __("Benchmark prices updated: {0}", [updated]),
								indicator: updated ? "green" : "orange",
							});
							if (state.benchmark) select.trigger("change");
						} catch (error) {
							frappe.show_alert({
								message: __("Benchmark price refresh failed."),
								indicator: "red",
							});
						} finally {
							btn.prop("disabled", false);
						}
					})
					.appendTo(controls);
			}
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

		function renderBenchmarkResult(out, r) {
			out.empty();
			const table = $('<table class="inv-table inv-benchmark-table">').appendTo(out);
			const tbody = $("<tbody>").appendTo(table);
			const row = (label, value, cls) => {
				const tr = $("<tr>").appendTo(tbody);
				$("<td>").text(label).appendTo(tr);
				$("<td>").addClass("inv-num").addClass(cls || "").text(value).appendTo(tr);
			};
			row(__("{0} (YTD)", [r.benchmark_name || r.benchmark]), inv.formatPercent(r.benchmark_return_ytd),
				inv.signClass(r.benchmark_return_ytd));
			row(__("This portfolio (TWR, YTD)"), inv.formatPercent(r.portfolio_twr_ytd),
				inv.signClass(r.portfolio_twr_ytd));
			if (r.excess_return_ytd !== null && r.excess_return_ytd !== undefined) {
				row(__("Excess vs benchmark"), inv.formatPercent(r.excess_return_ytd),
					inv.signClass(r.excess_return_ytd));
			}
			if (r.note) $('<p class="inv-muted">').text(r.note).appendTo(out);
			if (r.benchmark_currency && r.base_currency && r.benchmark_currency !== r.base_currency) {
				$('<p class="inv-section-sub">')
					.text(__("Benchmark converted from {0} to {1} at your FX rates.", [r.benchmark_currency, r.base_currency]))
					.appendTo(out);
			}
		}

		function renderUsage(data) {
			const license = data.license || {};
			const usage = data.usage || {};
			const used = usage.asset_classes_used || [];
			const max = license.max_asset_classes;
			const check = usage.value_check;
			const overClasses = max !== null && max !== undefined && used.length > max;
			const overValue = !!(check && check.breached);
			if (!overClasses && !overValue) return;
			const row = $('<div class="inv-upsell" role="alert">').appendTo(root);
			let message;
			if (overClasses) {
				message = __(
					"This site tracks {0} asset classes ({1}); your license covers {2}. Existing holdings stay visible — recording securities in new asset classes needs a higher tier.",
					[String(used.length), used.join(", "), String(max)]
				);
			} else if (check.reason === "missing_fx") {
				message = __(
					"Add an FX rate from {0} to {1} so the license's value cap can be checked; valuations continue meanwhile.",
					[check.base_currency || "", check.value_currency || ""]
				);
			} else {
				message = __(
					"This portfolio's value is above your license's cap of {0} {1}. Tracking continues; contact your vendor to raise the cap.",
					[check.max_value || "", check.value_currency || ""]
				);
			}
			$("<span>").text(message).appendTo(row);
			$("<a>")
				.attr("href", "#inv-license")
				.text(__("View license"))
				.on("click", (e) => {
					if (e && e.preventDefault) e.preventDefault();
					const target = root.find("#inv-license");
					const el = target && target.get ? target.get(0) : null;
					if (el && el.scrollIntoView) el.scrollIntoView({ behavior: "smooth" });
				})
				.appendTo(row);
		}

		function renderAllocation(data) {
			const values = data.values || {};
			const allocation = values.allocation_by_class || {};
			const entries = Object.entries(allocation)
				.map(([label, pct]) => [label, inv.num(pct) || 0])
				.sort((a, b) => b[1] - a[1]);
			const section = $('<section class="inv-section"></section>').appendTo(root);
			$("<h2>").text(__("Allocation")).appendTo(section);
			$('<p class="inv-section-sub">')
				.text(__("By asset class, % of portfolio value ({0}).", [values.base || ""]))
				.appendTo(section);
			if (!entries.length) {
				$('<p class="inv-muted">').text(__("No priced holdings yet.")).appendTo(section);
				return;
			}
			entries.forEach(([label, pct]) => {
				const row = $('<div class="inv-alloc-row">').appendTo(section);
				$('<span class="inv-alloc-label">').text(label).appendTo(row);
				const track = $('<div class="inv-alloc-track">').appendTo(row);
				$('<div class="inv-alloc-fill">')
					.css("width", `${Math.min(100, Math.max(0, pct))}%`)
					.attr("role", "img")
					.attr("aria-label", `${label} ${pct}%`)
					.appendTo(track);
				$('<span class="inv-alloc-pct">').text(`${pct.toFixed(2)}%`).appendTo(row);
			});
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
				await call("sync_now", { connection: conn.name });
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

		function renderLicense(data, parent) {
			const license = data.license || { tier: "standard", status: "none" };
			const section = $('<section class="inv-section inv-license" id="inv-license"></section>').appendTo(parent);
			$("<h2>").text(__("License")).appendTo(section);
			const tier = $('<div class="inv-license-tier">').appendTo(section);
			if (license.status === "none" || !license.status) {
				tier.text(__("Free tier"));
				$('<div class="inv-license-meta">')
					.text(__("One asset class, free forever. Enter a license key to track more asset classes or lift a portfolio-value cap."))
					.appendTo(section);
			} else if (license.status === "active") {
				tier.text(license.tier === "pro" ? __("Pro") : __("Standard"));
				const limits = license.max_asset_classes === null || license.max_asset_classes === undefined
					? __("unlimited asset classes")
					: __("{0} asset class(es)", [String(license.max_asset_classes)]);
				const cap = license.max_value
					? ` ${__("Value capped at {0} {1}.", [String(license.max_value), license.value_currency || ""])}`
					: "";
				$('<div class="inv-license-meta">')
					.text(
						__("Licensed to {0}.", [license.customer || __("Unknown customer")]) +
							` ${__("Covers {0}.", [limits])}` + cap +
							(license.expires ? ` ${__("Renews or expires on {0}.", [inv.formatDate(license.expires)])}` : "")
					)
					.appendTo(section);
			} else if (license.status === "expired") {
				tier.text(__("Free tier"));
				$('<div class="inv-warn" role="alert">')
					.text(__("License expired on {0}. The free tier's limits apply again; renew to restore your tier.", [inv.formatDate(license.expires)]))
					.appendTo(section);
			} else {
				tier.text(__("Free tier"));
				$('<div class="inv-warn" role="alert">')
					.text(__("The stored license key is invalid. Enter a valid key to enable your tier."))
					.appendTo(section);
			}
			if (inv.isManager()) {
				const actions = $('<div class="inv-license-actions">').appendTo(section);
				$('<button type="button" class="btn btn-default">')
					.text(__("Enter License Key"))
					.on("click", () => showLicenseDialog())
					.appendTo(actions);
			}
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

		function showLicenseDialog() {
			const dialog = new frappe.ui.Dialog({
				title: __("Enter License Key"),
				fields: [
					{ fieldname: "license_key", label: __("License Key"), fieldtype: "Small Text", reqd: 0,
						description: __("Paste the FINV1.… key you received. It is verified offline; clearing the field returns to the free Standard tier.") },
				],
				primary_action_label: __("Save"),
				primary_action: async (values) => {
					await call("save_license", { license_key: values.license_key || "" });
					dialog.hide();
					frappe.show_alert({ message: __("License saved."), indicator: "green" });
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
