/* Frappe Investing — Desk-wide namespace utilities.
 *
 * Loaded via app_include_js on every Desk page. This script registers
 * frappe.investing helpers only; it renders nothing and touches no routes,
 * so non-Investing Desk pages (and any non-Desk context) are unaffected.
 *
 * Every helper treats incoming values as untrusted: strings are escaped with
 * frappe.utils.escape_html before they may reach HTML, and numbers are parsed
 * defensively (the API serializes Decimal as string or float depending on the
 * value, so both are accepted).
 */
(() => {
	if (!window.frappe) return; // not a Desk context; stay silent

	const inv = (frappe.investing = frappe.investing || {});

	inv.escape = function (value) {
		const text = value === null || value === undefined ? "" : String(value);
		if (frappe.utils && typeof frappe.utils.escape_html === "function") {
			return frappe.utils.escape_html(text);
		}
		return text
			.replace(/&/g, "&amp;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;")
			.replace(/"/g, "&quot;")
			.replace(/'/g, "&#39;");
	};

	// Coerce API numerics (Decimal arrives as string or number) to Number|null.
	inv.num = function (value) {
		if (value === null || value === undefined || value === "") return null;
		const n = Number(value);
		return Number.isFinite(n) ? n : null;
	};

	// Money with currency prefix; null/NaN renders as an em dash.
	inv.formatMoney = function (value, currency) {
		const n = inv.num(value);
		if (n === null) return "—";
		if (typeof window.format_currency === "function") {
			try {
				return window.format_currency(n, currency || null);
			} catch (e) {
				/* fall through to the plain formatter */
			}
		}
		const grouped = n.toLocaleString(undefined, {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		});
		return currency ? `${currency} ${grouped}` : grouped;
	};

	// Quantity: up to 8 decimals preserved, trailing zeros trimmed by Number.
	inv.formatQty = function (value) {
		const n = inv.num(value);
		if (n === null) return "—";
		return n.toLocaleString(undefined, { maximumFractionDigits: 8 });
	};

	// Performance figures are ratios from the API (0.0523 → "5.23%").
	inv.formatPercent = function (ratio) {
		const n = inv.num(ratio);
		if (n === null) return "—";
		return `${(n * 100).toFixed(2)}%`;
	};

	// Signed colour coding is class-based only — never inline colours, so the
	// theme (incl. dark mode) owns the palette.
	inv.signClass = function (value) {
		const n = inv.num(value);
		if (n === null || n === 0) return "inv-zero";
		return n > 0 ? "inv-pos" : "inv-neg";
	};

	const STATUS_PILL = {
		Connected: "inv-pill-green",
		Error: "inv-pill-red",
		"Token Expired": "inv-pill-amber",
		"Not Connected": "inv-pill-gray",
		Posted: "inv-pill-green",
		Failed: "inv-pill-red",
		Pending: "inv-pill-amber",
		Skipped: "inv-pill-gray",
		"Not Applicable": "inv-pill-gray",
		Success: "inv-pill-green",
		Partial: "inv-pill-amber",
	};

	inv.statusPillClass = function (status) {
		return STATUS_PILL[status] || "inv-pill-gray";
	};

	// Desk form URL for a document, e.g. /app/investment-event/INV-0001.
	inv.formUrl = function (doctype, name) {
		const slug =
			frappe.router && typeof frappe.router.slug === "function"
				? frappe.router.slug(doctype)
				: String(doctype).toLowerCase().replace(/[\s_]+/g, "-");
		return `/app/${slug}/${encodeURIComponent(name)}`;
	};

	// Manager gate mirrors the server: Investment Manager or System Manager.
	inv.isManager = function () {
		const roles = frappe.user_roles || [];
		return roles.includes("Investment Manager") || roles.includes("System Manager");
	};

	inv.canRecord = function () {
		const roles = frappe.user_roles || [];
		return (
			roles.includes("Investment User") ||
			roles.includes("Investment Manager") ||
			roles.includes("System Manager")
		);
	};

	// Date/datetime display via Frappe's formatters when available.
	inv.formatDate = function (value) {
		if (!value) return "—";
		if (frappe.datetime && typeof frappe.datetime.str_to_user === "function") {
			try {
				return frappe.datetime.str_to_user(String(value).slice(0, 10));
			} catch (e) {
				/* fall through */
			}
		}
		return String(value);
	};

	inv.formatDatetime = function (value) {
		if (!value) return __("Never");
		if (frappe.datetime && typeof frappe.datetime.str_to_user === "function") {
			try {
				return frappe.datetime.str_to_user(String(value));
			} catch (e) {
				/* fall through */
			}
		}
		return String(value);
	};
})();
