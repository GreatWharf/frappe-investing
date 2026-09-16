"""Verify wheel and source archive contain every runtime source and asset."""

import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "frappe_investing"
SUFFIXES = {".py", ".json", ".js", ".css", ".txt", ".svg", ".html", ".csv"}


def main():
    expected = {
        str(path.relative_to(ROOT))
        for path in (ROOT / PACKAGE).rglob("*")
        if path.is_file() and path.suffix in SUFFIXES
    }
    wheels = list((ROOT / "dist").glob("*.whl"))
    sources = list((ROOT / "dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise SystemExit("Build into a clean dist directory; expected one wheel and one source archive.")
    with zipfile.ZipFile(wheels[0]) as archive:
        missing = expected - set(archive.namelist())
        if missing:
            raise SystemExit(f"Wheel missing runtime files: {sorted(missing)}")
    with tarfile.open(sources[0]) as archive:
        contents = {name.partition("/")[2] for name in archive.getnames()}
        missing = expected - contents
        if missing:
            raise SystemExit(f"Source archive missing runtime files: {sorted(missing)}")
    print(f"Verified {len(expected)} runtime files in wheel and source archive.")


if __name__ == "__main__":
    main()
