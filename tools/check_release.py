"""Validate release versions and the contents of built wheel/sdist archives."""

from __future__ import annotations

import argparse
from pathlib import Path
import tarfile
import zipfile

from cut_agent import __version__


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", nargs="?", default="dist")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    web_version = __import__("json").loads((root / "web/package.json").read_text())["version"]
    assert web_version == __version__, f"version mismatch: Python {__version__}, web {web_version}"
    archives = sorted(Path(args.dist).glob("cut_agent-*"))
    assert any(path.suffix == ".whl" for path in archives), "wheel missing"
    assert any(path.name.endswith(".tar.gz") for path in archives), "sdist missing"
    for path in archives:
        if path.suffix == ".whl":
            names = zipfile.ZipFile(path).namelist()
        elif path.name.endswith(".tar.gz"):
            names = tarfile.open(path, "r:gz").getnames()
        else:
            continue
        assert any(name.endswith("cut_agent/web_dist/index.html") for name in names), f"web UI missing from {path}"
    print(f"release {__version__}: versions and {len(archives)} archives verified")


if __name__ == "__main__":
    main()
