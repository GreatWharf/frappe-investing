import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "frappe_investing"
SCHEMA_DIR = APP / "investing" / "doctype"

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def test_frappe_dependency_range_has_stable_upper_bound():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["tool"]["bench"]["frappe-dependencies"]["frappe"] == ">=15.0.0-dev,<17.0.0"
    assert config["project"]["requires-python"] == ">=3.10,<3.15"


def test_schema_integrity():
    for folder in SCHEMA_DIR.iterdir():
        if not folder.is_dir() or folder.name == "__pycache__":
            continue
        doc = json.loads((folder / f"{folder.name}.json").read_text())
        assert doc["module"] == "Investing"
        order = doc["field_order"]
        assert len(order) == len(set(order)), folder.name
        assert set(order) == {f["fieldname"] for f in doc["fields"]}, folder.name
        if not doc.get("istable") and not doc.get("issingle"):
            assert doc["permissions"], folder.name


def test_event_schema_has_dedupe_and_submittable():
    doc = json.loads((SCHEMA_DIR / "investment_event/investment_event.json").read_text())
    fields = {f["fieldname"]: f for f in doc["fields"]}
    assert doc["is_submittable"] == 1
    assert fields["dedupe_key"]["unique"] == 1
    assert fields["dedupe_key"]["hidden"] == 1


def test_asset_classes_gated_in_controller_and_schema_allows_crypto():
    doc = json.loads((SCHEMA_DIR / "security/security.json").read_text())
    asset = next(f for f in doc["fields"] if f["fieldname"] == "asset_class")
    assert "Crypto" in asset["options"]
    controller = (SCHEMA_DIR / "security/security.py").read_text()
    assert "ManagedDocument" in controller
    assert "require_asset_class" in (ROOT / "frappe_investing/documents.py").read_text()


def test_python_310_grammar():
    import ast

    for source in list(APP.rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        ast.parse(source.read_text(), filename=str(source), feature_version=(3, 10))
