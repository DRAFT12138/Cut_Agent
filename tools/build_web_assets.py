"""Build the web client and copy it into the Python distribution package."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web" / "dist"
TARGET = ROOT / "src" / "cut_agent" / "web_dist"


def main() -> None:
    corepack = shutil.which("corepack.cmd" if os.name == "nt" else "corepack")
    if corepack is None:
        raise RuntimeError("corepack 未安装或不在 PATH")
    subprocess.run([corepack, "pnpm", "install", "--frozen-lockfile"], cwd=ROOT / "web", check=True)
    subprocess.run([corepack, "pnpm", "build"], cwd=ROOT / "web", check=True)
    shutil.rmtree(TARGET, ignore_errors=True)
    shutil.copytree(SOURCE, TARGET)
    print(f"Embedded web assets refreshed at {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
