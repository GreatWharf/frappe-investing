"""Static pre-install check: would `bench install-app` and `bench migrate` survive this tree?

There is no bench on a dev laptop, and the real install only happens in CI or on a
Frappe Cloud site. This catches the mistakes that break an install before they cost a
15-minute CI round trip: a hook pointing at a function that no longer exists, a doctype
JSON whose module or controller class is wrong, a Link field aimed at a doctype this app
never defines, a dashboard calling an API method that is not whitelisted.

Everything here is AST and JSON work — nothing is imported, so it runs without frappe.

    python scripts/preflight.py
"""

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "frappe_investing"
MODULE = APP / "investing"

# Doctypes this app links to but does not own. Anything outside this list that is not
# defined here is almost certainly a typo.
EXTERNAL_DOCTYPES = {
    "Account",
    "Company",
    "Cost Center",
    "Currency",
    "Customer",
    "File",
    "Item",
    "Journal Entry",
    "Party Type",
    "Role",
    "Supplier",
    "User",
}

problems = []


def fail(where, message):
    problems.append(f"{where}: {message}")


# ------------------------------------------------------------------ python surface
def module_symbols():
    """Top-level function/class names per dotted module path, without importing."""
    symbols = {}
    for path in APP.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        dotted = ".".join(parts)
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as exc:
            fail(str(path.relative_to(ROOT)), f"syntax error: {exc}")
            continue
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        symbols[dotted] = names
    return symbols


def whitelisted(path):
    """Names decorated with @frappe.whitelist in one file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(call, ast.Attribute) and call.attr == "whitelist":
                found.add(node.name)
    return found


def resolve(dotted, symbols):
    """Does `a.b.c` name a top-level symbol in module `a.b`?"""
    module, _, name = dotted.rpartition(".")
    return module in symbols and name in symbols[module]


# ------------------------------------------------------------------------- hooks
def hook_targets():
    """Every dotted app path mentioned in hooks.py, wherever it is nested."""
    source = (APP / "hooks.py").read_text()
    targets = set()
    for match in re.finditer(r'"(frappe_investing\.[A-Za-z0-9_.]+)"', source):
        targets.add(match.group(1))
    return targets


def check_hooks(symbols):
    for dotted in sorted(hook_targets()):
        if not resolve(dotted, symbols):
            fail("hooks.py", f"{dotted} does not exist")


def check_patches(symbols):
    for line in (APP / "patches.txt").read_text().splitlines():
        entry = line.strip()
        if not entry or entry.startswith(("#", "[")):
            continue
        dotted = entry.split()[0]
        if dotted.endswith(".execute"):
            dotted = dotted[: -len(".execute")]
            if dotted not in symbols:
                fail("patches.txt", f"module {dotted} does not exist")
            elif "execute" not in symbols[dotted]:
                fail("patches.txt", f"{dotted} has no execute()")
        elif not resolve(dotted, symbols):
            fail("patches.txt", f"{dotted} does not exist")


# ---------------------------------------------------------------------- doctypes
def snake(name):
    return name.lower().replace(" ", "_").replace("-", "_")


def camel(name):
    return "".join(part for part in re.split(r"[\s_-]+", name) if part)


def load_doctypes():
    """{name: (json, dir)} for every doctype JSON in the app."""
    found = {}
    for path in (MODULE / "doctype").glob("*/*.json"):
        if path.stem != path.parent.name:
            continue  # dashboard/ and other sidecar JSON
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            fail(str(path.relative_to(ROOT)), f"invalid JSON: {exc}")
            continue
        if data.get("doctype") != "DocType":
            continue
        found[data.get("name", path.stem)] = (data, path.parent)
    return found


def check_doctypes(doctypes, symbols):
    modules = {line.strip() for line in (APP / "modules.txt").read_text().splitlines() if line.strip()}
    owned = set(doctypes) | EXTERNAL_DOCTYPES
    for name, (data, folder) in sorted(doctypes.items()):
        where = f"doctype/{folder.name}"
        if data.get("module") not in modules:
            fail(where, f"module {data.get('module')!r} is not in modules.txt")
        # frappe builds controller paths as {app}.{scrub(module)}.doctype.X.x — so the
        # module dir must be named after the scrubbed Module Def, not the app.
        if snake(data.get("module") or "") != MODULE.name:
            fail(where, f"module {data.get('module')!r} scrubs to {snake(data.get('module') or '')!r}, but the dir is {MODULE.name!r}")
        if snake(name) != folder.name:
            fail(where, f"folder should be {snake(name)} for doctype {name!r}")

        # Frappe imports <folder>/<folder>.py and expects a class named after the doctype.
        if not data.get("is_virtual"):
            controller = folder / f"{folder.name}.py"
            if not controller.exists():
                fail(where, f"missing controller {controller.name}")
            else:
                dotted = ".".join(controller.relative_to(ROOT).with_suffix("").parts)
                if camel(name) not in symbols.get(dotted, set()):
                    fail(where, f"{controller.name} has no class {camel(name)}")

        for field in data.get("fields") or []:
            kind, target = field.get("fieldtype"), (field.get("options") or "").strip()
            fieldname = field.get("fieldname")
            if kind in {"Link", "Table", "Table MultiSelect"}:
                if not target:
                    fail(where, f"{kind} field {fieldname} has no options")
                elif target not in owned:
                    fail(where, f"field {fieldname} links to unknown doctype {target!r}")
            if kind in {"Table", "Table MultiSelect"} and target in doctypes:
                if not doctypes[target][0].get("istable"):
                    fail(where, f"field {fieldname} uses {target!r}, which is not a child table")
            if kind == "Select" and target and target not in owned and "\n" not in target:
                fail(where, f"Select field {fieldname} has a single option {target!r}")

        for row in data.get("permissions") or []:
            if not row.get("role"):
                fail(where, "a permission row has no role")


# ------------------------------------------------------------- dashboard <-> api
def check_dashboard_calls():
    api = APP / "api.py"
    exposed = whitelisted(api)
    page_js = MODULE / "page" / "investing" / "investing.js"
    called = set(re.findall(r'call\(\s*"([a-z_]+)"', page_js.read_text()))
    called |= {
        m.rpartition(".")[2]
        for m in re.findall(r'"(frappe_investing\.api\.[a-z_]+)"', page_js.read_text())
    }
    for name in sorted(called - exposed):
        fail("page/investing/investing.js", f"calls api.{name}, which is not whitelisted")


def check_assets():
    for asset in ("js/investing.js", "css/investing.css", "images/investing.svg"):
        if not (APP / "public" / asset).exists():
            fail("hooks.py", f"public/{asset} is referenced but missing")


# ------------------------------------------------------------------ fixtures
def check_fixtures():
    """Native cards/charts only ship if they import on install AND migrate.

    frappe's sync_for() walks IMPORTABLE_DOCTYPES per module dir (workspace and
    dashboard_chart_source included; number_card/dashboard_chart/dashboard are
    NOT), while sync_dashboards() looks up "dashboard chart"/"number card"
    with spaces — but apps ship underscore dirs, so on v15 those never import.
    The version-proof path is frappe_investing/fixtures/*.json via the fixtures
    hook (sync_fixtures on every migrate). Every native card/chart/dashboard in
    the module dirs must therefore be mirrored there, and everything mirrored
    must parse with module Investing.
    """
    mirrored = {}
    for path in (APP / "fixtures").glob("*.json"):
        try:
            mirrored[path.name] = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            fail(f"fixtures/{path.name}", f"invalid JSON: {exc}")
    for sub in ("number_card", "dashboard_chart", "investing_dashboard"):
        for path in (MODULE / sub).glob("*/*.json"):
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                fail(str(path.relative_to(ROOT)), f"invalid JSON: {exc}")
                continue
            if data.get("module") != "Investing":
                continue
            if data.get("doctype") not in {"Number Card", "Dashboard Chart", "Dashboard"}:
                continue
            name = f"{data['doctype'].lower().replace(' ', '_')}_{path.parent.name}.json"
            if name not in mirrored:
                fail(
                    str(path.relative_to(ROOT)),
                    f"not mirrored in fixtures/{name} — it would never import "
                    "(sync_for skips this doctype, sync_dashboards wants space-named dirs)",
                )
            elif mirrored[name] != data:
                fail(f"fixtures/{name}", "drifted from the module-dir source — re-mirror it")
    for name, data in mirrored.items():
        if data.get("module") != "Investing":
            fail(f"fixtures/{name}", "module is not Investing — the fixtures hook would ship another app's doc")
        if data.get("doctype") == "Dashboard Chart" and data.get("is_standard"):
            # frappe's fixtures hook calls import_doc(data_import=True), which
            # does NOT set ignore_validate, and DashboardChart.validate throws
            # "Cannot edit Standard charts" whenever developer_mode is off —
            # i.e. on every production install. Dev benches (developer_mode=1)
            # hide this, so it must be caught here.
            fail(f"fixtures/{name}", "Dashboard Chart fixtures must not be is_standard — production installs reject them")


def main():
    symbols = module_symbols()
    doctypes = load_doctypes()
    check_hooks(symbols)
    check_patches(symbols)
    check_doctypes(doctypes, symbols)
    check_dashboard_calls()
    check_assets()
    check_fixtures()
    if problems:
        print(f"{len(problems)} problem(s) would break the install:\n")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"Preflight clean: {len(doctypes)} doctypes, {len(hook_targets())} hook targets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
