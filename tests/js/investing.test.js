/* Behavioural tests for the Frappe Investing Desk frontend.
 *
 * Run with no installs and no network:
 *   node --test tests/js/investing.test.js
 *
 * The Desk (frappe, jQuery, frappe.ui.Dialog, timers) is replaced by the
 * dependency-free harness in ./dom-harness.cjs; the app scripts under test
 * are the real files, executed unchanged in a vm context.
 */

const assert = require("node:assert/strict");
const test = require("node:test");

const { createDesk, Wrap } = require("./dom-harness.cjs");

const NAMESPACE_JS = "frappe_investing/public/js/investing.js";
const PAGE_JS = "frappe_investing/investing/page/investing/investing.js";
const SETUP_JS = "frappe_investing/investing/page/broker_setup/broker_setup.js";
const FORM_JS = "frappe_investing/investing/doctype/broker_connection/broker_connection.js";

const clone = (value) => JSON.parse(JSON.stringify(value));

function dashboardFixture() {
	return {
		needs_setup: false,
		portfolio: "PF-0001",
		portfolios: [
			{ name: "PF-0001", portfolio_name: "Family", company: "Co", base_currency: "INR" },
			{ name: "PF-0002", portfolio_name: "Trading", company: "Co", base_currency: "INR" },
		],
		values: {
			day: "2026-09-16",
			base: "INR",
			by_security: {
				"SEC-RELIANCE": { qty: "10", cost: "20000", market_value: "28000.25", unrealized_pnl: "8000.25", asset_class: "Stock" },
				"SEC-BADBOND": { qty: "5", cost: "5000", market_value: null, unrealized_pnl: "0", asset_class: "Bond" },
			},
			stale: ["SEC-BADBOND"],
			total_value: "28000.25",
			total_cost: "25000",
			unrealized_pnl: "3000.25",
			allocation_by_class: { Stock: "100.00" },
		},
		performance: { twr_ytd: "0.0523", xirr_ytd: null, realized_pnl_ytd: "1250.50", income_ytd: "420.00", snapshots: 30 },
		connections: [
			{ name: "CONN-Z", connection_name: "Zerodha Main", broker: "Zerodha", status: "Connected", enabled: 1, last_sync: "2026-09-16 09:30:00", last_error: "" },
			{ name: "CONN-A", connection_name: "Alpaca US", broker: "Alpaca", status: "Error", enabled: 0, last_sync: null, last_error: "unauthorized" },
		],
		pending_accounting: [
			{ name: "EV-0009", event_type: "Dividend", posting_date: "2026-09-15", security: "SEC-RELIANCE" },
		],
		accounts: [
			{ name: "IA-0001", account_name: "Family Main", portfolio: "PF-0001", currency: "INR" },
			{ name: "IA-0002", account_name: "Trading Main", portfolio: "PF-0002", currency: "INR" },
		],
		recent_events: [
			{ name: "EV-0008", event_type: "Buy", posting_date: "2026-09-14", security: "SEC-RELIANCE", qty: "10", price: "2000", currency: "INR", accounting_status: "Posted" },
			{ name: "EV-0007", event_type: "Deposit", posting_date: "2026-09-13", security: null, qty: null, price: null, currency: "INR", accounting_status: "Pending" },
		],
		license: { tier: "standard", status: "none", customer: "", expires: "", max_asset_classes: 1, max_value: null, value_currency: null },
		usage: { asset_classes_used: ["Stock", "Bond"], value_check: null },
		settings: { price_provider: "Stooq", auto_accounting: 1, default_cost_method: "FIFO" },
	};
}

/* Responder factory: handlers per method, everything else fails loudly so a
 * wrong call from the page can never pass silently. */
function makeResponder(handlers = {}) {
	return (method, args) => {
		for (const [key, fn] of Object.entries(handlers)) {
			if (method === key || method.endsWith(key)) return fn(args, method);
		}
		throw new Error(`unexpected frappe.call in test: ${method} ${JSON.stringify(args)}`);
	};
}

function deskWithDashboard({ roles = ["Investment Manager"], dashboard, extra = {} } = {}) {
	const data = dashboard || dashboardFixture();
	const desk = createDesk({
		roles,
		defaultCompany: "Co",
		responder: makeResponder({
			// Echo the requested portfolio, as the real API does.
			"frappe_investing.api.get_dashboard": (args) => {
				const reply = clone(data);
				if (args && args.portfolio) reply.portfolio = args.portfolio;
				return { message: reply };
			},
			"frappe_investing.api.benchmark_list": () => ({
				message: [
					{ code: "SP500", name: "S&P 500", currency: "USD" },
					{ code: "NIFTY50", name: "Nifty 50", currency: "INR" },
				],
			}),
			"frappe_investing.api.compare_benchmark": (args) => ({
				message: {
					benchmark: args.benchmark,
					benchmark_name: args.benchmark === "NIFTY50" ? "Nifty 50" : "S&P 500",
					benchmark_currency: args.benchmark === "NIFTY50" ? "INR" : "USD",
					base_currency: "INR",
					portfolio_twr_ytd: "0.0523",
					benchmark_return_ytd: "0.0310",
					excess_return_ytd: "0.0213",
					note: null,
				},
			}),
			...extra,
		}),
	});
	desk.loadScript(NAMESPACE_JS);
	desk.loadScript(PAGE_JS);
	return desk;
}

async function loadDashboardPage(desk) {
	const wrapper = {};
	desk.frappe.pages["investing"].on_page_load(wrapper);
	await desk.flush();
	return wrapper;
}

function installChartSpy(desk, { fail = false } = {}) {
	const seen = [];
	const Chart = fail
		? class { constructor() { throw new Error("no chart"); } }
		: class { constructor(el, opts) { seen.push({ el, opts }); } };
	desk.window.Chart = Chart;
	return seen;
}

function chartData(inv, r) {
	// inv.benchmarkChartData runs inside the vm realm, whose Array prototype
	// differs from this realm's; round-trip before any deep assertion.
	return JSON.parse(JSON.stringify(inv.benchmarkChartData(r)));
}

test("namespace utilities escape, format and classify", () => {
	const desk = deskWithDashboard();
	const inv = desk.frappe.investing;
	assert.equal(inv.escape('<img src=x onerror="pwn">'), "&lt;img src=x onerror=&quot;pwn&quot;&gt;");
	assert.equal(inv.formatMoney(null, "INR"), "—");
	assert.equal(inv.formatMoney("28000.25", "INR"), "INR 28,000.25");
	assert.equal(inv.formatPercent("0.0523"), "5.23%");
	assert.equal(inv.formatPercent(null), "—");
	assert.equal(inv.signClass("3"), "inv-pos");
	assert.equal(inv.signClass("-0.5"), "inv-neg");
	assert.equal(inv.signClass(0), "inv-zero");
	assert.equal(inv.formUrl("Investment Event", "EV/0001"), "/app/investment-event/EV%2F0001");
	assert.ok(inv.isManager());
});

test("dashboard renders native sections and keeps holdings sortable", async () => {
	const desk = deskWithDashboard();
	await loadDashboardPage(desk);

	// KPI grid, allocation bars and the license card are gone: Desk-native
	// list views own the numbers, so the page keeps only what Desk cannot do.
	// ("Unrealized P&L" still names a holdings column — the KPI card is gone.)
	for (const label of ["Portfolio Value", "YTD TWR", "YTD XIRR", "Allocation"]) {
		assert.ok(!desk.texts().includes(label), `custom section removed: ${label}`);
	}
	assert.equal(desk.find(".inv-kpi").length, 0);
	assert.equal(desk.find(".inv-alloc-fill").length, 0);
	assert.equal(desk.find(".inv-upsell").length, 0);

	// What stays: picker toolbar, sortable holdings, broker connections.
	assert.equal(desk.find("#inv-portfolio-select").length, 1);
	assert.ok(desk.button("Record Event"));
	const headings = desk.texts();
	for (const h of ["Holdings", "Broker Connections", "Pending Accounting", "Recent Events"]) {
		assert.ok(headings.includes(h), `section kept: ${h}`);
	}

	// Holdings: stale bond sinks below the priced row under value-desc default.
	const bodyRows = desk.find("tbody").toArray()[0].children;
	assert.equal(bodyRows[0].children[0].allText(), "SEC-RELIANCE");
	assert.equal(bodyRows[1].children[0].allText(), "SEC-BADBOND");

	// Clicking a column header re-sorts (security ascending here).
	const securityBtn = desk.buttons().find((el) => el.allText().includes("Security"));
	assert.ok(securityBtn, "sortable Security header");
	securityBtn.click();
	await desk.flush();
	const resorted = desk.find("tbody").toArray()[0].children;
	assert.equal(resorted[0].children[0].allText(), "SEC-BADBOND");
	assert.equal(resorted[1].children[0].allText(), "SEC-RELIANCE");
});

test("holdings table keeps value columns for priced rows", async () => {
	const desk = deskWithDashboard();
	await loadDashboardPage(desk);
	const bodyRows = desk.find("tbody").toArray()[0].children;
	const first = bodyRows[0].children;
	assert.equal(first[1].textValue, "10"); // qty
	assert.ok(first[3].textValue.includes("28,000.25"), "market value formatted");
	assert.ok(first[4].textValue.includes("20,000"), "cost formatted");
});

test("stale holding shows a Stale badge instead of a price", async () => {
	const desk = deskWithDashboard();
	await loadDashboardPage(desk);
	const badge = desk.find(".inv-badge-stale").get(0);
	assert.ok(badge, "stale badge rendered");
	assert.equal(badge.textValue, "Stale");
	// The stale row keeps dashes for value-derived cells rather than zeros.
	const row = badge.parent.parent;
	assert.equal(row.children[3].textValue, "—"); // market value
	assert.equal(row.children[5].textValue.includes("0.00"), true); // unrealized P&L is 0, not missing
});

test("benchmark selection renders a portfolio-vs-benchmark line chart", async () => {
	const desk = deskWithDashboard();
	const charts = installChartSpy(desk);
	await loadDashboardPage(desk);

	// The picker is populated from benchmark_list with a neutral placeholder first.
	const selectEl = desk.find("select").toArray().find((el) =>
		el.descendants().some((d) => d.tag === "option" && d.textValue === "Nifty 50")
	);
	assert.ok(selectEl, "benchmark select rendered with catalog options");
	const optionValues = selectEl.descendants()
		.filter((d) => d.tag === "option")
		.map((d) => d.attributes.value);
	assert.deepEqual(optionValues, ["", "SP500", "NIFTY50"]);
	// Nothing selected yet: an explanatory hint, no comparison call.
	assert.ok(desk.texts().some((t) => /Pick an index to compare/.test(t)));
	assert.equal(desk.callsTo("compare_benchmark").length, 0);

	// Selecting an index calls compare_benchmark and draws the two-series line.
	selectEl.value = "NIFTY50";
	selectEl.trigger("change");
	await desk.flush();
	assert.equal(desk.callsTo("compare_benchmark").length, 1);
	assert.equal(desk.callsTo("compare_benchmark")[0].args.benchmark, "NIFTY50");
	assert.equal(charts.length, 1, "one frappe-charts line chart constructed");
	const chart = charts[0].opts;
	assert.equal(chart.type, "line");
	const series = Object.fromEntries(
		JSON.parse(JSON.stringify(chart.data.datasets)).map((d) => [d.name, d.values])
	);
	assert.deepEqual(series["This portfolio (TWR, YTD)"], [0, 5.23]);
	assert.deepEqual(series["Nifty 50"], [0, 3.1]);
	assert.ok(desk.texts().some((t) => /Excess vs benchmark: 2.13%/.test(t)));

	// Clearing the selection returns to the hint without a further call.
	selectEl.value = "";
	selectEl.trigger("change");
	await desk.flush();
	assert.equal(desk.callsTo("compare_benchmark").length, 1, "no call without a selection");
});

test("benchmark chart builder passes nulls through without fabricating", () => {
	const desk = deskWithDashboard();
	const inv = desk.frappe.investing;
	assert.ok(typeof inv.benchmarkChartData === "function");
	const full = chartData(inv, {
		benchmark: "NIFTY50", benchmark_name: "Nifty 50",
		portfolio_twr_ytd: "0.0523", benchmark_return_ytd: "0.0310",
	});
	assert.equal(full.datasets.length, 2);
	const partial = chartData(inv, {
		benchmark: "SP500", benchmark_name: "S&P 500",
		portfolio_twr_ytd: "0.0523", benchmark_return_ytd: null,
	});
	assert.equal(partial.datasets.length, 1, "missing benchmark series stays missing");
	assert.deepEqual(partial.datasets[0].values, [0, 5.23]);
});

test("benchmark chart failure degrades to a message, not a blank page", async () => {
	const desk = deskWithDashboard();
	installChartSpy(desk, { fail: true });
	await loadDashboardPage(desk);
	const selectEl = desk.find("select").toArray().find((el) =>
		el.descendants().some((d) => d.tag === "option" && d.textValue === "Nifty 50")
	);
	selectEl.value = "NIFTY50";
	selectEl.trigger("change");
	await desk.flush();
	assert.ok(desk.texts().some((t) => /could not be rendered/.test(t)));
});

test("benchmark note renders when index prices are missing", async () => {
	const desk = deskWithDashboard({
		extra: {
			"frappe_investing.api.compare_benchmark": (args) => ({
				message: {
					benchmark: args.benchmark,
					benchmark_name: "S&P 500",
					benchmark_currency: "USD",
					base_currency: "INR",
					portfolio_twr_ytd: "0.0523",
					benchmark_return_ytd: null,
					excess_return_ytd: null,
					note: "No stored prices for S&P 500; refresh benchmark prices first.",
				},
			}),
		},
	});
	await loadDashboardPage(desk);
	const selectEl = desk.find("select").toArray().find((el) =>
		el.descendants().some((d) => d.tag === "option" && d.textValue === "S&P 500")
	);
	selectEl.value = "SP500";
	selectEl.trigger("change");
	await desk.flush();
	assert.ok(
		desk.texts().some((t) => /No stored prices for S&P 500/.test(t)),
		"missing-prices note shown instead of a fabricated number"
	);
	assert.equal(desk.find(".inv-benchmark-chart").length, 0, "no chart without benchmark data");
});

test("toolbar actions stay gated while license chrome is gone", async () => {
	const user = deskWithDashboard({ roles: ["Investment User"] });
	await loadDashboardPage(user);
	assert.ok(user.button("Record Event"), "users keep the Record Event action");
	assert.equal(user.button("Refresh Prices"), null);
	assert.equal(user.button("Sync Now"), null, "sync is manager-only server-side");
	assert.equal(user.button("Enter License Key"), null, "license chrome removed");
	assert.equal(user.find(".inv-license").length, 0, "no license section");

	const manager = deskWithDashboard();
	await loadDashboardPage(manager);
	assert.ok(manager.button("Record Event"));
	assert.ok(manager.button("Refresh Prices"), "managers keep price refresh");
	assert.ok(manager.button("Sync Now"), "managers keep per-connection sync");
});

test("event dialog adapts visible fields to the event type", async () => {
	const desk = deskWithDashboard();
	await loadDashboardPage(desk);
	desk.button("Record Event").click();
	const dialog = desk.lastDialog();
	assert.equal(dialog.title, "Record Investment Event");

	const visible = (name) => !dialog.get_df(name).hidden;
	// Buy (default): security, qty, price, fees, taxes
	for (const f of ["security", "qty", "price", "fees", "taxes"]) assert.ok(visible(f), `Buy shows ${f}`);
	for (const f of ["amount", "gross", "split_ratio", "child_security", "target_currency"]) assert.ok(!visible(f), `Buy hides ${f}`);

	dialog.set_value("event_type", "Dividend");
	for (const f of ["security", "gross", "taxes"]) assert.ok(visible(f), `Dividend shows ${f}`);
	for (const f of ["qty", "price", "amount"]) assert.ok(!visible(f), `Dividend hides ${f}`);
	assert.equal(dialog.get_df("gross").reqd, 1);

	dialog.set_value("event_type", "Deposit");
	assert.ok(visible("amount"));
	for (const f of ["security", "qty", "gross"]) assert.ok(!visible(f), `Deposit hides ${f}`);

	dialog.set_value("event_type", "Split");
	for (const f of ["security", "split_ratio"]) assert.ok(visible(f), `Split shows ${f}`);
	assert.ok(!visible("qty"));

	dialog.set_value("event_type", "Spin-off");
	for (const f of ["security", "child_security", "basis_allocation", "child_ratio"]) {
		assert.ok(visible(f), `Spin-off shows ${f}`);
	}

	dialog.set_value("event_type", "FX Conversion");
	for (const f of ["amount", "target_currency", "target_amount"]) assert.ok(visible(f), `FX shows ${f}`);
	assert.ok(!visible("security"));
});

test("invalid numeric input is rejected client-side before the API call", async () => {
	const desk = deskWithDashboard({
		extra: { "frappe_investing.api.record_manual_event": () => ({ message: { name: "EV-1", created: true } }) },
	});
	await loadDashboardPage(desk);
	desk.button("Record Event").click();
	const dialog = desk.lastDialog();
	dialog.set_value("account", "IA-0001");
	dialog.set_value("posting_date", "2026-09-16");
	dialog.set_value("currency", "INR");
	dialog.set_value("security", "SEC-RELIANCE");
	dialog.set_value("qty", -5);
	dialog.set_value("price", 2000);
	await assert.rejects(() => dialog.primary());
	assert.ok(desk.recorded.throws.some((m) => /positive quantity/.test(m)));
	assert.equal(desk.callsTo("record_manual_event").length, 0, "no API call for invalid input");

	dialog.set_value("qty", 10);
	await dialog.primary();
	await desk.flush();
	const recorded = desk.callsTo("record_manual_event");
	assert.equal(recorded.length, 1);
	assert.equal(recorded[0].args.event_type, "Buy");
	assert.equal(recorded[0].args.qty, 10);
	assert.equal(dialog.visible, false);
});

test("Sync Now disables while the call is in flight and ignores repeat clicks", async () => {
	let release;
	const desk = deskWithDashboard({
		extra: {
			"frappe_investing.api.sync_now": () => new Promise((resolve) => { release = resolve; }),
		},
	});
	await loadDashboardPage(desk);
	const button = desk.buttons().find((el) => el.attributes["aria-label"] === "Sync Now Zerodha Main");
	assert.ok(button, "per-connection Sync Now button");

	button.click();
	button.click(); // second click while in flight must not re-call
	await desk.flush();
	assert.equal(desk.callsTo("sync_now").length, 1);
	assert.equal(desk.callsTo("sync_now")[0].args.connection, "CONN-Z");
	assert.equal(button.props.disabled, true, "disabled while in flight");

	release({ message: { queued: true } });
	await desk.flush();
	assert.equal(button.props.disabled, false, "re-enabled after the call settles");
	assert.ok(desk.recorded.alerts.some((a) => /Sync queued/.test(a.message)));
	assert.equal(desk.timers.count(), 1, "status poll interval started after queueing");
});

test("Sync Now reports honestly when the job was deduplicated away", async () => {
	const desk = deskWithDashboard({
		extra: {
			"frappe_investing.api.sync_now": () => ({
				message: { queued: false, note: "A sync for this connection is already queued or running." },
			}),
		},
	});
	await loadDashboardPage(desk);
	const button = desk.buttons().find((el) => el.attributes["aria-label"] === "Sync Now Zerodha Main");
	button.click();
	await desk.flush();
	assert.ok(
		desk.recorded.alerts.some((a) => /already queued or running/.test(a.message)),
		"dedup note shown instead of a false queued confirmation"
	);
	assert.equal(desk.timers.count(), 1, "poll still starts to watch the running sync");
});

test("post-sync poll reloads the dashboard when the connection status changes", async () => {
	const desk = deskWithDashboard({
		extra: {
			"frappe_investing.api.sync_now": () => ({ message: { queued: true } }),
			"frappe.client.get_value": () => ({
				message: { status: "Connected", last_sync: "2026-09-16 10:05:00" },
			}),
		},
	});
	const wrapper = await loadDashboardPage(desk);
	desk.buttons().find((el) => el.attributes["aria-label"] === "Sync Now Zerodha Main").click();
	await desk.flush();
	assert.equal(desk.callsTo("get_dashboard").length, 1);
	const timerId = wrapper.investing_state.syncPollTimer;
	assert.ok(timerId, "interval id recorded on page state");

	await desk.timers.tick(timerId);
	await desk.flush();
	assert.equal(desk.timers.count(), 0, "poll cleared once status changed");
	assert.equal(desk.callsTo("get_dashboard").length, 2, "dashboard reloaded after sync finished");
	assert.ok(desk.recorded.alerts.some((a) => /Sync finished/.test(a.message)));

	// And on page unload (router change) any active poll is cleared.
	desk.buttons().find((el) => el.attributes["aria-label"] === "Sync Now Zerodha Main").click();
	await desk.flush();
	assert.equal(desk.timers.count(), 1);
	desk.triggerRouteChange();
	assert.equal(desk.timers.count(), 0, "router change clears the poll interval");
});

test("switching portfolios refetches the dashboard", async () => {
	const desk = deskWithDashboard();
	const wrapper = await loadDashboardPage(desk);
	assert.equal(desk.callsTo("get_dashboard").length, 1);

	const select = desk.find("#inv-portfolio-select");
	assert.equal(select.length, 1);
	select.val("PF-0002");
	select.trigger("change");
	await desk.flush();
	const calls = desk.callsTo("get_dashboard");
	assert.equal(calls.length, 2, "refetch on portfolio change");
	assert.equal(calls[1].args.portfolio, "PF-0002");
	assert.equal(wrapper.investing_state.portfolio, "PF-0002");
});

test("empty state renders the setup CTA and creates a portfolio", async () => {
	const empty = {
		needs_setup: true,
		license: { tier: "standard", status: "none", customer: "", expires: "" },
	};
	const desk = deskWithDashboard({
		dashboard: empty,
		extra: { "frappe_investing.api.create_portfolio": (args) => ({ message: { name: "PF-NEW", ...args } }) },
	});
	await loadDashboardPage(desk);
	assert.ok(desk.button("Create Portfolio"), "setup CTA visible to a manager");
	assert.ok(desk.button("Open Broker Setup"), "link to the guided connector page");
	assert.equal(desk.find(".inv-license").length, 0, "no license chrome on the empty state");

	desk.button("Create Portfolio").click();
	const dialog = desk.lastDialog();
	assert.deepEqual(
		Array.from(dialog.fields, (f) => f.fieldname),
		["portfolio_name", "company", "base_currency"]
	);
	dialog.set_value("portfolio_name", "Family Office");
	dialog.set_value("company", "Co");
	dialog.set_value("base_currency", "INR");
	await dialog.primary();
	await desk.flush();
	const created = desk.callsTo("create_portfolio");
	assert.equal(created.length, 1);
	assert.equal(created[0].args.portfolio_name, "Family Office");
	assert.equal(desk.callsTo("get_dashboard").length, 2, "dashboard reloaded after creation");
});

test("empty state tells non-managers to ask a manager instead", async () => {
	const desk = deskWithDashboard({
		roles: ["Investment User"],
		dashboard: { needs_setup: true, license: { tier: "standard", status: "none" } },
	});
	await loadDashboardPage(desk);
	assert.equal(desk.button("Create Portfolio"), null);
	assert.ok(desk.texts().some((t) => /Ask an Investment Manager/.test(t)));
});

/* -------------------------------------------------- broker setup page */
function deskWithBrokerSetup({ roles = ["Investment Manager"], connections = [], extra = {} } = {}) {
	const desk = createDesk({
		roles,
		responder: makeResponder({
			"frappe.client.get_list": () => ({ message: clone(connections) }),
			...extra,
		}),
	});
	desk.loadScript(NAMESPACE_JS);
	desk.loadScript(SETUP_JS);
	return desk;
}

test("broker setup shows all four connectors with honest capability badges", async () => {
	const desk = deskWithBrokerSetup();
	desk.frappe.pages["broker-setup"].on_page_load({});
	await desk.flush();
	const texts = desk.texts();
	for (const title of ["Zerodha Kite Connect", "Alpaca", "Interactive Brokers Flex", "CSV Import"]) {
		assert.ok(texts.includes(title), `card: ${title}`);
	}
	for (const badge of ["Holdings ✓", "Quotes ✓", "Today's trades ✓", "History via CSV", "No dividend feed"]) {
		assert.ok(texts.includes(badge), `Zerodha badge: ${badge}`);
	}
	assert.ok(texts.some((t) => /Flex token and a saved Flex query/.test(t)), "IBKR Flex explanation");
	assert.ok(texts.some((t) => /dry run before you commit/.test(t)), "CSV dry-run honesty");

	desk.button("Open Import Batches").click();
	assert.deepEqual(desk.recorded.routes.at(-1), ["List", "Import Batch"]);

	desk.button("New Interactive Brokers Connection").click();
	assert.deepEqual(desk.recorded.newDocs.at(-1), {
		doctype: "Broker Connection",
		opts: { broker: "Interactive Brokers" },
	});
});

test("Zerodha connect flow opens the login URL and exchanges the pasted token", async () => {
	const connections = [
		{ name: "CONN-Z", connection_name: "Z Main", broker: "Zerodha", status: "Token Expired", enabled: 1, last_sync: null },
	];
	const desk = deskWithBrokerSetup({
		connections,
		extra: {
			"frappe_investing.api.zerodha_login_url": () => ({ message: { url: "https://kite.zerodha.com/connect/login?api_key=K&v=3" } }),
			"frappe_investing.api.zerodha_exchange_token": () => ({ message: { connected: true } }),
		},
	});
	desk.frappe.pages["broker-setup"].on_page_load({});
	await desk.flush();

	assert.ok(desk.texts().includes("Token Expired"), "existing connection shows its status");
	desk.button("Connect with Zerodha").click();
	await desk.flush();
	assert.equal(desk.recorded.openedWindows.length, 1);
	assert.ok(desk.recorded.openedWindows[0].url.startsWith("https://kite.zerodha.com/"));

	const dialog = desk.lastDialog();
	assert.equal(dialog.title, "Complete Zerodha Login");
	dialog.set_value("request_token", "tok-123");
	await dialog.primary();
	await desk.flush();
	const exchanged = desk.callsTo("zerodha_exchange_token");
	assert.equal(exchanged.length, 1);
	assert.deepEqual(exchanged[0].args, { connection: "CONN-Z", request_token: "tok-123" });
});

test("broker setup hides Zerodha connect from non-managers", async () => {
	const desk = deskWithBrokerSetup({
		roles: ["Investment User"],
		connections: [{ name: "CONN-Z", connection_name: "Z Main", broker: "Zerodha", status: "Connected", enabled: 1, last_sync: null }],
	});
	desk.frappe.pages["broker-setup"].on_page_load({});
	await desk.flush();
	assert.equal(desk.button("Connect with Zerodha"), null);
});

/* ---------------------------------------------------- broker form JS */
function runConnectionForm(desk, doc, { isNew = false, dirty = false } = {}) {
	const buttons = [];
	const toggles = [];
	const headlines = [];
	const primary = [];
	const frm = {
		doc,
		is_new: () => isNew,
		is_dirty: () => dirty,
		toggle_display: (fieldname, visible) => toggles.push([fieldname, visible]),
		set_df_property: () => {},
		add_custom_button: (label, fn, group) => buttons.push({ label, fn, group }),
		change_custom_button_type: (label) => primary.push(label),
		dashboard: { set_headline_alert: (message, color) => headlines.push({ message, color }) },
		reload_doc() {
			frm.reloaded = true;
		},
	};
	desk.frappe.ui.form.handlers["Broker Connection"].refresh(frm);
	return { frm, buttons, toggles, headlines, primary };
}

function deskWithForm() {
	const desk = createDesk({ roles: ["Investment Manager"], responder: makeResponder({}) });
	desk.loadScript(NAMESPACE_JS);
	desk.loadScript(FORM_JS);
	return desk;
}

test("broker connection form toggles credential fields per broker", () => {
	const desk = deskWithForm();
	const zerodha = runConnectionForm(desk, { name: "CONN-Z", broker: "Zerodha", enabled: 1, status: "Connected" });
	const shown = new Map(zerodha.toggles);
	assert.equal(shown.get("api_key"), true);
	assert.equal(shown.get("api_secret"), true);
	assert.equal(shown.get("access_token"), true);
	assert.equal(shown.get("flex_token"), false);
	assert.equal(shown.get("flex_query_id"), false);
	assert.equal(shown.get("sandbox"), false);

	const ibkr = runConnectionForm(desk, { name: "CONN-I", broker: "Interactive Brokers", enabled: 0, status: "Not Connected" });
	const ibkrShown = new Map(ibkr.toggles);
	assert.equal(ibkrShown.get("flex_token"), true);
	assert.equal(ibkrShown.get("flex_query_id"), true);
	assert.equal(ibkrShown.get("api_key"), false);

	const csv = runConnectionForm(desk, { name: "CONN-C", broker: "CSV Import", enabled: 0, status: "Not Connected" });
	assert.ok(csv.toggles.every(([, visible]) => visible === false), "CSV hides all credential fields");
});

test("broker connection buttons, dirty guard and honest headline", () => {
	const desk = deskWithForm();
	const expired = runConnectionForm(desk, {
		name: "CONN-Z", broker: "Zerodha", enabled: 1, status: "Token Expired", last_error: "",
	});
	const labels = expired.buttons.map((b) => b.label);
	assert.ok(labels.includes("Connect"), "Zerodha offers Connect");
	assert.ok(labels.includes("Sync Now"));
	assert.ok(labels.includes("View Sync Logs"));
	assert.ok(labels.includes("View Events"));
	assert.deepEqual(expired.primary, ["Sync Now"]);
	assert.ok(/tokens expire/i.test(expired.headlines[0].message), "daily-token honesty headline");
	assert.equal(expired.headlines[0].color, "orange");

	expired.buttons.find((b) => b.label === "View Sync Logs").fn();
	assert.deepEqual(desk.recorded.routes.at(-1), ["List", "Broker Sync Log", { connection: "CONN-Z" }]);
	expired.buttons.find((b) => b.label === "View Events").fn();
	assert.deepEqual(desk.recorded.routes.at(-1), ["List", "Investment Event", { connection: "CONN-Z" }]);

	// CSV Import events carry no connection link, so its View Events keeps the source filter.
	const csv = runConnectionForm(desk, { name: "CONN-C", broker: "CSV Import", enabled: 1, status: "Connected" });
	csv.buttons.find((b) => b.label === "View Events").fn();
	assert.deepEqual(desk.recorded.routes.at(-1), ["List", "Investment Event", { source: "CSV Import" }]);

	// Dirty form guard: no API call, a save-first message instead.
	const dirty = runConnectionForm(desk, { name: "CONN-Z", broker: "Zerodha", enabled: 1, status: "Connected" }, { dirty: true });
	dirty.buttons.find((b) => b.label === "Sync Now").fn();
	assert.equal(desk.callsTo("sync_now").length, 0);
	assert.ok(desk.recorded.msgprints.some((m) => /Save your changes first/.test(m)));

	// Alpaca never offers Connect; disabled connections never offer Sync Now.
	const alpaca = runConnectionForm(desk, { name: "CONN-A", broker: "Alpaca", enabled: 0, status: "Error", last_error: "bad key" });
	assert.ok(!alpaca.buttons.some((b) => b.label === "Connect"));
	assert.ok(!alpaca.buttons.some((b) => b.label === "Sync Now"));
	assert.ok(/bad key/.test(alpaca.headlines[0].message));
});

/* ------------------------------------------------- statement import tabs */

function importTabLink(desk, label) {
	return desk.find("a").toArray().find((el) => el.allText() === label) || null;
}

const IMPORT_REFERENCE = [
	{ type: "Buy", required: ["security_key", "qty", "price"], optional: ["fees"] },
	{ type: "Dividend", required: ["security_key", "gross"], optional: ["taxes / withholding"] },
	{ type: "FX Conversion", required: ["amount", "target_currency", "target_amount"], optional: [] },
];

function deskWithImport({ preview, postResult, costMethod } = {}) {
	const desk = deskWithDashboard({
		extra: {
			"frappe_investing.api.import_reference": () => ({ message: { event_types: clone(IMPORT_REFERENCE) } }),
			"frappe_investing.api.import_template_url": () => ({
				message: { url: "/assets/frappe_investing/csv/statement_template.csv" },
			}),
			"frappe_investing.api.import_cost_method": () => ({
				message: costMethod || { portfolio: "PF-0001", portfolio_cost_method: "FIFO", effective: "FIFO" },
			}),
			"frappe_investing.api.import_preview": () => ({ message: clone(preview) }),
			"frappe_investing.api.import_post": () => ({ message: clone(postResult) }),
		},
	});
	desk.window.FileReader = class {
		readAsText() { this.onload({ target: { result: "" } }); }
	};
	return desk;
}

const GOOD_PREVIEW = {
	account: "IA-0001",
	total_rows: 2,
	valid_rows: 2,
	error_rows: 0,
	by_type: { Buy: 1, Dividend: 1 },
	events: [],
	errors: [],
	batch: null,
};

const BAD_PREVIEW = {
	account: "IA-0001",
	total_rows: 2,
	valid_rows: 1,
	error_rows: 1,
	by_type: { Buy: 1 },
	events: [],
	errors: [{ row: 3, message: "Invalid date '2026-13-40' (want YYYY-MM-DD)" }],
	batch: "IMP-0001",
};

test("statement import section renders two tabs with the import tab active", async () => {
	const desk = deskWithImport();
	await loadDashboardPage(desk);
	const headings = desk.texts();
	assert.ok(headings.includes("Statement Import"), "import section after holdings");
	assert.ok(importTabLink(desk, "Import Statement"), "tab 1");
	assert.ok(importTabLink(desk, "Supported Operations"), "tab 2");
	// Import tab active by default: account picker + dropzone visible.
	assert.equal(desk.find("#inv-import-account").length, 1);
	assert.ok(desk.find(".inv-dropzone").length >= 1);
	assert.ok(desk.button("Download CSV Template"));
});

test("import tab is portfolio-aware and surfaces the cost method", async () => {
	const desk = deskWithImport({
		costMethod: { portfolio: "PF-0001", portfolio_cost_method: null, effective: "AVERAGE" },
	});
	await loadDashboardPage(desk);
	// Only this portfolio's accounts are offered.
	const options = desk.find("#inv-import-account").get(0).descendants()
		.filter((d) => d.tag === "option")
		.map((d) => d.attributes.value);
	assert.deepEqual(options, ["IA-0001"]);
	await desk.flush();
	const costCalls = desk.callsTo("import_cost_method");
	assert.equal(costCalls.length, 1);
	assert.equal(costCalls[0].args.account, "IA-0001");
	assert.ok(desk.texts().some((t) => /default AVERAGE/.test(t)), "default-method note documents the selector");
});

test("template download opens the app-served CSV", async () => {
	const desk = deskWithImport();
	await loadDashboardPage(desk);
	desk.button("Download CSV Template").click();
	await desk.flush();
	assert.equal(desk.callsTo("import_template_url").length, 1);
	assert.equal(desk.recorded.openedWindows.length, 1);
	assert.equal(
		desk.recorded.openedWindows[0].url,
		"/assets/frappe_investing/csv/statement_template.csv"
	);
});

test("clean preview shows counts and posts through import_post", async () => {
	const desk = deskWithImport({
		preview: GOOD_PREVIEW,
		postResult: { account: "IA-0001", posted: 2, created: 2, errors: [] },
	});
	const wrapper = await loadDashboardPage(desk);
	// Simulate a chosen file: FileReader feeds importText, then the pane
	// re-renders and the preview call runs.
	wrapper.investing_state.importText = "date,type\n2026-01-15,Buy";
	wrapper.investing_state.importFileName = "stmt.csv";
	wrapper.investing_reload();
	await desk.flush();
	const previews = desk.callsTo("import_preview");
	assert.equal(previews.length, 1);
	assert.equal(previews[0].args.account, "IA-0001");
	assert.ok(desk.texts().some((t) => /2 rows: 2 valid, 0 with errors/.test(t)), "preview counts shown");
	assert.ok(desk.button("Post 2 Events"), "post CTA appears only on a clean preview");

	desk.button("Post 2 Events").click();
	await desk.flush();
	const posts = desk.callsTo("import_post");
	assert.equal(posts.length, 1);
	assert.equal(posts[0].args.account, "IA-0001");
	assert.ok(desk.recorded.alerts.some((a) => /Posted 2 events/.test(a.message)));
	assert.equal(desk.callsTo("get_dashboard").length, 3, "dashboard reloaded after posting");
});

test("error preview lists per-row errors and offers no post button", async () => {
	const desk = deskWithImport({ preview: BAD_PREVIEW });
	const wrapper = await loadDashboardPage(desk);
	wrapper.investing_state.importText = "date,type\n2026-13-40,Buy";
	wrapper.investing_reload();
	await desk.flush();
	assert.equal(desk.callsTo("import_preview").length, 1);
	assert.ok(desk.texts().some((t) => /2 rows: 1 valid, 1 with errors/.test(t)));
	assert.ok(desk.texts().some((t) => /2026-13-40/.test(t)), "row error message shown");
	assert.ok(desk.texts().some((t) => /IMP-0001/.test(t)), "batch link for the failed preview");
	assert.equal(desk.button("Post 1 Events"), null, "no post CTA while errors remain");
	assert.equal(desk.callsTo("import_post").length, 0, "nothing posted");
});

test("reference tab switches panes and lists required columns per operation", async () => {
	const desk = deskWithImport();
	await loadDashboardPage(desk);
	assert.equal(desk.callsTo("import_reference").length, 1, "reference loads with the section");
	importTabLink(desk, "Supported Operations").click();
	await desk.flush();
	assert.ok(desk.texts().some((t) => /taxes \/ withholding/.test(t)), "dividend withholding documented");
	assert.ok(desk.texts().some((t) => /target_currency/.test(t)), "FX legs documented");
	importTabLink(desk, "Import Statement").click();
	await desk.flush();
	assert.equal(desk.find("#inv-import-account").length, 1, "switching back restores the import pane");
});
