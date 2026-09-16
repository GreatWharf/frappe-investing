app_name = "frappe_investing"
app_title = "Investing"
app_publisher = "Great Wharf"
app_description = (
    "Portfolios, broker sync, corporate actions, performance and investment accounting for ERPNext"
)
app_email = "Merrick@greatwharf.com"
app_license = "MIT"
app_home = "/desk/investing"
app_logo_url = "/assets/frappe_investing/images/investing.svg"
required_apps = ["erpnext"]

add_to_apps_screen = [
    dict(
        name=app_name,
        title=app_title,
        logo=app_logo_url,
        route=app_home,
        has_permission="frappe_investing.api.has_permission",
    )
]
app_include_js = ["/assets/frappe_investing/js/investing.js"]
app_include_css = ["/assets/frappe_investing/css/investing.css"]

before_install = "frappe_investing.install.check_versions"
after_install = "frappe_investing.install.after_install"
after_migrate = "frappe_investing.install.after_migrate"
before_uninstall = "frappe_investing.install.before_uninstall"

scheduler_events = {
    "cron": {"*/15 * * * *": ["frappe_investing.sync_service.scheduled_broker_sync"]},
    "daily": ["frappe_investing.services.daily_snapshots", "frappe_investing.license_service.check_expiry"],
}

permission_query_conditions = {
    "Portfolio": "frappe_investing.permissions.scoped_query",
    "Broker Connection": "frappe_investing.permissions.scoped_query",
    "Investment Accounting Policy": "frappe_investing.permissions.scoped_query",
    "Investment Event": "frappe_investing.permissions.child_query",
}
