frappe.provide('frappe.dashboards.chart_sources');

frappe.dashboards.chart_sources["Portfolio Allocation"] = {
	method: "frappe_investing.allocation_chart.get_data",
	filters: [
		{
			fieldname: "portfolio",
			label: __("Portfolio"),
			fieldtype: "Link",
			options: "Portfolio"
		}
	]
};
