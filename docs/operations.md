# Operations

## Requirements

- Frappe + ERPNext, matching majors v15 or v16, MariaDB.
- Python 3.10–3.14. Runtime deps: `requests`, `cryptography`.

## Install (self-hosted bench)

```sh
bench get-app <repo-url>
bench --site <site> install-app frappe_investing
```

On Frappe Cloud, add the app from the Marketplace once listed; installation and migrations are handled by the platform. No manual migration steps exist beyond normal `bench migrate` on update.

## Roles

- **Investment User** — read portfolios, enter manual events, view dashboards.
- **Investment Manager** — connections, policies, imports, prices, license, corrections.
- **System Manager** — everything, including uninstall guards.

## Scheduled jobs

- Broker sync: every 15 minutes, enabled connections only, one per connection at a time (lock + job dedupe).
- Daily: portfolio snapshots, license expiry check.

## Backups and data

Investment events, lots and journal-entry links are accounting data — back up the database and site encryption key. `before_uninstall` refuses to uninstall with submitted events; export first or archive the site.

## Upgrades

Standard `bench update` / Cloud deploy. Schema changes ship as DocType migrations; no manual data fixes are expected. Breaking changes will be called out in release notes with a migration path.
