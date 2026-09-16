/* Minimal Desk harness for Frappe Investing UI tests.
 *
 * Dependency-free: no jsdom, no npm packages, no network. Provides just
 * enough of the jQuery / frappe.ui surface that the app scripts use, so
 * behaviour (not pixels) can be asserted under `node --test`.
 *
 * Usage:
 *   const { createDesk } = require("./dom-harness.cjs");
 *   const desk = createDesk({ roles: ["Investment Manager"], responder });
 *   desk.loadScript("frappe_investing/public/js/investing.js");
 *   desk.loadScript("frappe_investing/frappe_investing/page/investing/investing.js");
 *   desk.frappe.pages["investing"].on_page_load({});
 *   await desk.flush();
 */

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const REPO = path.resolve(__dirname, "..", "..");

/* ---------------------------------------------------------------- DOM */
class Element {
	constructor(tag) {
		this.tag = tag;
		this.attributes = {};
		this.classes = new Set();
		this.children = [];
		this.parent = null;
		this.handlers = {};
		this.props = {};
		this.style = {};
		this.textValue = "";
		this.value = "";
		this.scrolledIntoView = false;
	}

	allText() {
		return this.textValue + this.children.map((child) => child.allText()).join("");
	}

	descendants() {
		return this.children.flatMap((child) => [child, ...child.descendants()]);
	}

	matches(selector) {
		// Supported simple selectors: tag, .class, #id, tag.class
		let tag = null;
		let cls = null;
		let id = null;
		if (selector.startsWith(".")) cls = selector.slice(1);
		else if (selector.startsWith("#")) id = selector.slice(1);
		else if (selector.includes(".")) [tag, cls] = selector.split(".");
		else tag = selector;
		if (tag && this.tag !== tag) return false;
		if (cls && !this.classes.has(cls)) return false;
		if (id && this.attributes.id !== id) return false;
		return true;
	}

	scrollIntoView() {
		this.scrolledIntoView = true;
	}

	trigger(event, arg) {
		for (const handler of this.handlers[event] || []) {
			handler.call(this, { preventDefault() {}, type: event }, arg);
		}
	}

	click() {
		this.trigger("click");
	}
}

function parseMarkup(markup) {
	const open = markup.match(/^<([a-zA-Z0-9]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*\/?>/);
	if (!open) throw new Error(`harness cannot parse markup: ${markup}`);
	const el = new Element(open[1].toLowerCase());
	const attrRe = /([a-zA-Z-]+)(?:="([^"]*)")?/g;
	let m;
	while ((m = attrRe.exec(open[2]))) {
		if (!m[2]) continue;
		el.attributes[m[1]] = m[2];
		if (m[1] === "class") m[2].split(/\s+/).filter(Boolean).forEach((c) => el.classes.add(c));
	}
	return el;
}

class Wrap {
	constructor(elements) {
		this.elements = elements;
	}

	get length() {
		return this.elements.length;
	}

	get(index) {
		return this.elements[index] || null;
	}

	eq(index) {
		return new Wrap(this.elements[index] ? [this.elements[index]] : []);
	}

	first() {
		return this.eq(0);
	}

	toArray() {
		return this.elements.slice();
	}

	_each(fn) {
		this.elements.forEach(fn);
		return this;
	}

	appendTo(parent) {
		const target = parent instanceof Wrap ? parent.elements[0] : parent;
		if (!target || !target.children) throw new Error("appendTo target is not a harness element");
		return this._each((el) => {
			el.parent = target;
			target.children.push(el);
		});
	}

	append(child) {
		const childEl = child instanceof Wrap ? child.elements[0] : child;
		this._each((el) => {
			childEl.parent = el;
			el.children.push(childEl);
		});
		return this;
	}

	text(value) {
		if (value === undefined) return this.elements.map((el) => el.allText()).join("");
		return this._each((el) => {
			el.textValue = value === null || value === undefined ? "" : String(value);
		});
	}

	html(value) {
		if (value === undefined) return this.elements[0] ? this.elements[0].textValue : "";
		return this.text(value); // markup is never injected with data; store as text
	}

	addClass(names) {
		return this._each((el) =>
			String(names || "").split(/\s+/).filter(Boolean).forEach((c) => el.classes.add(c))
		);
	}

	removeClass(names) {
		return this._each((el) =>
			String(names || "").split(/\s+/).filter(Boolean).forEach((c) => el.classes.delete(c))
		);
	}

	toggleClass(name, force) {
		return this._each((el) => {
			const on = force === undefined ? !el.classes.has(name) : !!force;
			if (on) el.classes.add(name);
			else el.classes.delete(name);
		});
	}

	hasClass(name) {
		return this.elements.some((el) => el.classes.has(name));
	}

	attr(name, value) {
		if (value === undefined) return this.elements[0] ? this.elements[0].attributes[name] : undefined;
		return this._each((el) => {
			if (value === null) delete el.attributes[name];
			else el.attributes[name] = String(value);
		});
	}

	removeAttr(name) {
		return this._each((el) => delete el.attributes[name]);
	}

	prop(name, value) {
		if (value === undefined) return this.elements[0] ? this.elements[0].props[name] : undefined;
		return this._each((el) => {
			el.props[name] = value;
		});
	}

	val(value) {
		if (value === undefined) return this.elements[0] ? this.elements[0].value : undefined;
		return this._each((el) => {
			el.value = value;
		});
	}

	css(name, value) {
		if (value === undefined) return this.elements[0] ? this.elements[0].style[name] : undefined;
		return this._each((el) => {
			el.style[name] = value;
		});
	}

	on(event, handler) {
		return this._each((el) => {
			(el.handlers[event] = el.handlers[event] || []).push(handler);
		});
	}

	trigger(event, arg) {
		this.elements.forEach((el) => {
			for (const handler of el.handlers[event] || []) {
				handler.call(el, { preventDefault() {}, type: event }, arg);
			}
		});
		return this;
	}

	click() {
		return this.trigger("click");
	}

	empty() {
		return this._each((el) => {
			el.children = [];
			el.textValue = "";
		});
	}

	remove() {
		return this._each((el) => {
			if (el.parent) el.parent.children = el.parent.children.filter((c) => c !== el);
			el.parent = null;
		});
	}

	find(selector) {
		const found = [];
		for (const el of this.elements) {
			for (const desc of el.descendants()) {
				if (desc.matches(selector)) found.push(desc);
			}
		}
		return new Wrap(found);
	}

	filter(fn) {
		return new Wrap(this.elements.filter((el, i) => fn.call(el, el, i)));
	}

	is(selector) {
		return this.elements.some((el) => el.matches(selector));
	}
}

function makeDollar() {
	const dollar = (input) => {
		if (input instanceof Wrap) return input;
		if (input instanceof Element) return new Wrap([input]);
		if (typeof input === "string" && input.trim().startsWith("<")) return new Wrap([parseMarkup(input.trim())]);
		throw new Error(`harness $ cannot handle: ${typeof input}`);
	};
	dollar.Element = Element;
	dollar.Wrap = Wrap;
	return dollar;
}

/* ------------------------------------------------------------ Dialog */
class FakeControl {
	constructor(df) {
		this.df = df;
		this.value = df.default === undefined ? "" : df.default;
	}

	set_value(value, silent) {
		this.value = value;
		if (!silent && this.df.onchange) this.df.onchange();
	}

	get_value() {
		return this.value;
	}

	refresh() {}
}

class FakeDialog {
	constructor(opts) {
		this.opts = opts;
		this.title = opts.title;
		this.fields = opts.fields || [];
		this.fields_dict = {};
		this.visible = false;
		this.propertyLog = [];
		for (const df of this.fields) {
			this.fields_dict[df.fieldname] = new FakeControl(df);
		}
	}

	get_value(fieldname) {
		const control = this.fields_dict[fieldname];
		return control ? control.get_value() : undefined;
	}

	set_value(fieldname, value) {
		const control = this.fields_dict[fieldname];
		if (control) control.set_value(value);
		return Promise.resolve();
	}

	get_values() {
		const out = {};
		for (const df of this.fields) {
			if (df.fieldtype === "HTML" || !df.fieldname) continue;
			if (df.hidden) continue;
			out[df.fieldname] = this.get_value(df.fieldname);
		}
		return out;
	}

	set_df_property(fieldname, prop, value) {
		const df = this.fields.find((f) => f.fieldname === fieldname);
		if (df) {
			df[prop] = value;
			this.propertyLog.push([fieldname, prop, value]);
		}
	}

	get_df(fieldname) {
		return this.fields.find((f) => f.fieldname === fieldname);
	}

	show() {
		this.visible = true;
	}

	hide() {
		this.visible = false;
		if (this.opts.on_hide) this.opts.on_hide();
	}

	/* test helper: run the primary action the way Desk would */
	primary() {
		return this.opts.primary_action(this.get_values());
	}

	secondary() {
		if (this.opts.secondary_action) return this.opts.secondary_action(this.get_values());
		return undefined;
	}
}

/* ------------------------------------------------------------- frappe */
function translate(text, args) {
	let out = String(text);
	(args || []).forEach((value, i) => {
		out = out.split(`{${i}}`).join(String(value));
	});
	return out;
}

/* Plain data crossing the vm boundary keeps the vm realm's prototypes, which
 * breaks assert.deepStrictEqual; clone recorded payloads into this realm. */
const hostClone = (value) => (value === undefined ? value : JSON.parse(JSON.stringify(value)));

function createFrappe(options) {
	const recorded = {
		calls: [],
		routes: [],
		alerts: [],
		msgprints: [],
		throws: [],
		newDocs: [],
		openedWindows: [],
		appPages: [],
		dialogs: [],
		routeOptions: undefined,
	};

	const frappe = {
		boot: { user: { roles: options.roles } },
		user_roles: options.roles.slice(),
		pages: {},
		session: { user: "test@example.com" },
		route_options: undefined,

		ui: {
			Dialog: class extends FakeDialog {
				constructor(opts) {
					super(opts);
					recorded.dialogs.push(this);
				}
			},
			make_app_page(opts) {
				const main = new Element("div");
				const page = {
					main: new Wrap([main]),
					title: opts.title,
					set_title(t) {
						page.title = t;
					},
					set_primary_action(label, fn) {
						page.primaryAction = { label, fn };
					},
					add_menu_item(label, fn) {
						(page.menuItems = page.menuItems || []).push({ label, fn });
					},
					_main: main,
				};
				recorded.appPages.push(page);
				return page;
			},
			form: {
				handlers: {},
				on(doctype, handlers) {
					frappe.ui.form.handlers[doctype] = handlers;
				},
				make_control({ df }) {
					return new FakeControl(df);
				},
			},
		},

		utils: {
			escape_html(value) {
				return String(value)
					.replace(/&/g, "&amp;")
					.replace(/</g, "&lt;")
					.replace(/>/g, "&gt;")
					.replace(/"/g, "&quot;")
					.replace(/'/g, "&#39;");
			},
		},

		datetime: {
			get_today: () => options.today,
			now_datetime: () => `${options.today} 12:00:00`,
			str_to_user: (value) => String(value),
			add_days: (date, days) => date,
		},

		defaults: {
			get_user_default: () => options.defaultCompany || null,
		},

		router: {
			slug: (name) => String(name).toLowerCase().replace(/[\s_]+/g, "-"),
			_changeCallbacks: [],
			on(event, callback) {
				if (event === "change") frappe.router._changeCallbacks.push(callback);
				return () => {
					frappe.router._changeCallbacks = frappe.router._changeCallbacks.filter((c) => c !== callback);
				};
			},
		},

		set_route(...args) {
			recorded.routes.push(hostClone(args));
		},

		new_doc(doctype, opts) {
			recorded.newDocs.push(hostClone({ doctype, opts }));
		},

		show_alert(message, seconds) {
			recorded.alerts.push(typeof message === "string" ? { message } : hostClone(message));
		},

		msgprint(message) {
			recorded.msgprints.push(message);
		},

		throw(message) {
			recorded.throws.push(message);
			throw new Error(typeof message === "string" ? message : "frappe.throw");
		},

		call(callOpts) {
			recorded.calls.push({ ...callOpts, args: hostClone(callOpts.args || {}) });
			const result = options.responder(callOpts.method, callOpts.args || {}, callOpts);
			return Promise.resolve(result).then((r) => {
				if (r && typeof r === "object" && "message" in r) return r;
				return { message: r };
			});
		},
	};

	return { frappe, recorded };
}

/* -------------------------------------------------------------- desk */
function createDesk({ roles = ["Investment Manager"], today = "2026-09-16", responder, defaultCompany } = {}) {
	if (!responder) throw new Error("createDesk requires a responder(method, args)");
	const $ = makeDollar();
	const { frappe, recorded } = createFrappe({ roles, today, responder, defaultCompany });

	// Controllable intervals so sync polling can be tested without real timers.
	const timers = {
		nextId: 1,
		active: new Map(),
		setInterval(fn, ms) {
			const id = timers.nextId++;
			timers.active.set(id, { fn, ms });
			return id;
		},
		clearInterval(id) {
			timers.active.delete(id);
		},
		async tick(id) {
			const entry = id !== undefined ? timers.active.get(id) : null;
			const entries = entry ? [[id, entry]] : Array.from(timers.active.entries());
			for (const [, timer] of entries) {
				await timer.fn();
			}
		},
		count() {
			return timers.active.size;
		},
	};

	const windowObj = {
		frappe,
		open(url, target) {
			recorded.openedWindows.push({ url, target });
		},
	};

	const context = vm.createContext({
		frappe,
		$,
		__: translate,
		window: windowObj,
		setInterval: timers.setInterval,
		clearInterval: timers.clearInterval,
		console,
	});

	function loadScript(relPath) {
		const abs = path.join(REPO, relPath);
		const code = fs.readFileSync(abs, "utf8");
		vm.runInContext(code, context, { filename: relPath });
	}

	async function flush(rounds = 8) {
		for (let i = 0; i < rounds; i++) {
			await new Promise((resolve) => setImmediate(resolve));
		}
	}

	return {
		frappe,
		$,
		recorded,
		timers,
		context,
		loadScript,
		flush,
		window: windowObj,

		/* convenience queries */
		main() {
			return recorded.appPages[recorded.appPages.length - 1].main;
		},
		allElements() {
			const main = recorded.appPages[recorded.appPages.length - 1]._main;
			return [main, ...main.descendants()];
		},
		find(selector) {
			return this.main().find(selector);
		},
		buttons() {
			return this.find("button").toArray();
		},
		button(label) {
			const found = this.buttons().find((el) => el.allText() === label);
			return found || null;
		},
		lastDialog() {
			return recorded.dialogs[recorded.dialogs.length - 1] || null;
		},
		callsTo(methodSuffix) {
			return recorded.calls.filter((c) => c.method.endsWith(methodSuffix));
		},
		texts() {
			return this.allElements().map((el) => el.textValue).filter(Boolean);
		},
		triggerRouteChange() {
			for (const cb of frappe.router._changeCallbacks.slice()) cb();
		},
	};
}

module.exports = { createDesk, Element, Wrap, FakeDialog, translate };
