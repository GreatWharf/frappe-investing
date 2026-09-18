"""Allocation donut source: market-value share grouped by Security.asset_class.

Backs the "Portfolio Allocation" Dashboard Chart (Dashboard Chart Source).
"""

import frappe
from frappe.utils.dashboard import cache_source


@frappe.whitelist()
@cache_source
def get_data(
	chart_name=None,
	chart=None,
	no_cache=None,
	filters=None,
	from_date=None,
	to_date=None,
	timespan=None,
	time_interval=None,
	heatmap_year=None,
):
	if chart_name:
		chart = frappe.get_doc("Dashboard Chart", chart_name)
	else:
		chart = frappe._dict(frappe.parse_json(chart))

	portfolio = None
	if filters:
		filters = frappe.parse_json(filters)
		if isinstance(filters, list) and filters:
			filters = filters[0]
		portfolio = (filters or {}).get("portfolio")

	if not portfolio:
		portfolios = frappe.get_all("Portfolio", pluck="name", limit_page_length=1)
		portfolio = portfolios[0] if portfolios else None

	labels = []
	values = []

	if portfolio:
		from frappe_investing.services import value_portfolio

		result = value_portfolio(portfolio)
		allocation = result.get("allocation_by_class") or {}
		for bucket, pct in sorted(allocation.items(), key=lambda item: item[1], reverse=True):
			labels.append(bucket)
			values.append(float(pct))

	return {
		"labels": labels,
		"datasets": [{"name": "Allocation %", "chartType": "bar", "values": values}],
	}
