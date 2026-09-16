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
const PAGE_JS = "frappe_investing/frappe_investing/page/investing/investing.js";
const SETUP_JS = "frappe_investing/frappe_investing/page/broker_setup/broker_setup.js";
const FORM_JS = "frappe_investing/frappe_investing/doctype/broker_connection/broker_connection.js";

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
		performance: { twr_ytd: "0.0523", xirr_ytd: null, snapshots: 30 },
		connections: [
			{ name: "CONN-Z", connection_name: "Zerodha Main", broker: "Zerodha", status: "Connected", enabled: 1, last_sync: "2026-09-16 09:30:00", last_error: "" },
			{ name: "CONN-A", connection_name: "Alpaca US", broker: "Alpaca", status: "Error", enabled: 0, last_sync: null, last_error: "unauthorized" },
		],
		pending_accounting: [
			{ name: "EV-0009", event_type: "Dividend", posting_date: "2026-09-15", security: "SEC-RELIANCE" },
		],
		recent_events: [
			{ name: "EV-0008", event_type: "Buy", posting_date: "2026-09-14", security: "SEC-RELIANCE", qty: "10", price: "2000", currency: "INR", accounting_status: "Posted" },
			{ name: "EV-0007", event_type: "Deposit", posting_date: "2026-09-13", security: null, qty: null, price: null, currency: "INR", accounting_status: "Pending" },
		],
		license: { tier: "standard", status: "none", customer: "", expires: "" },
		crypto_enabled: false,
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

function kpiCard(desk, label) {
	const labelEl = desk
		.find(".inv-kpi-label")
		.toArray()
		.find((el) => el.textValue === label);
	assert.ok(labelEl, `KPI card not found: ${label}`);
	const card = labelEl.parent;
	const valueEl = card.children.find((child) => child.classes.has("inv-kpi-value"));
	return { card, valueEl };
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

test("dashboard renders KPI cards from the get_dashboard payload", async () => {
	const desk = deskWithDashboard();
	await loadDashboardPage(desk);

	assert.equal(kpiCard(desk, "Portfolio Value").valueEl.textValue, "INR 28,000.25");
	assert.equal(kpiCard(desk, "YTD TWR").valueEl.textValue, "5.23%");
	assert.ok(kpiCard(desk, "YTD TWR").valueEl.classes.has("inv-pos"));
	// XIRR is null in the payload → em dash, no fabricated number.
	assert.equal(kpiCard(desk, "YTD XIRR").valueEl.textValue, "—");
	const unrealized = kpiCard(desk, "Unrealized P&L").valueEl;
	assert.equal(unrealized.textValue, "INR 3,000.25");
	assert.ok(unrealized.classes.has("inv-pos"));
	// The frozen API does not expose these; the cards must degrade to a dash.
	assert.equal(kpiCard(desk, "Realized P&L (YTD)").valueEl.textValue, "—");
	assert.equal(kpiCard(desk, "Income (YTD)").valueEl.textValue, "—");

	// Allocation bar and upsell row.
	const fill = desk.find(".inv-alloc-fill").get(0);
	assert.ok(fill, "allocation bar rendered");
	assert.equal(fill.style.width, "100%");
	assert.ok(desk.texts().includes("Crypto holdings — Pro tier"));

	// Holdings: stale bond sinks below the priced row under value-desc default.
	const bodyRows = desk.find("tbody").toArray()[0].children;
	assert.equal(bodyRows[0].children[0].allText(), "SEC-RELIANCE");
	assert.equal(bodyRows[1].children[0].allText(), "SEC-BADBOND");
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

test("license dialog is gated to managers", async () => {
	const user = deskWithDashboard({ roles: ["Investment User"] });
	await loadDashboardPage(user);
	assert.equal(user.button("Enter License Key"), null);
	assert.ok(user.button("Record Event"), "users keep the Record Event action");
	assert.equal(user.button("Refresh Prices"), null);
	assert.equal(user.button("Sync Now"), null, "sync is manager-only server-side");

	const manager = deskWithDashboard({
		extra: { "frappe_investing.api.save_license": (args) => ({ message: { tier: "pro", status: "active", ...args } }) },
	});
	await loadDashboardPage(manager);
	const button = manager.button("Enter License Key");
	assert.ok(button, "manager sees the license action");
	button.click();
	const dialog = manager.lastDialog();
	assert.equal(dialog.title, "Enter License Key");
	dialog.set_value("license_key", "FINV1.payload.sig");
	await dialog.primary();
	await manager.flush();
	const save = manager.callsTo("save_license");
	assert.equal(save.length, 1);
	assert.equal(save[0].args.license_key, "FINV1.payload.sig");
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
	assert.ok(desk.texts().some((t) => /Standard \(free\)/.test(t)), "license card still renders");

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
