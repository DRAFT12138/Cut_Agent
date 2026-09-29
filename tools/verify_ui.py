"""Single entry point for offline browser smoke and regression suites."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


SUITES = {
    "smoke": ["verify_document_navigation_ui.py"],
    "regression": [
        "verify_cross_tab_edit_ui.py",
        "verify_reconnect_ui.py",
        "verify_review_ui.py",
        "verify_service_restart_ui.py",
        "verify_stage_links_ui.py",
        "verify_true_ab_ui.py",
    ],
    "external": ["verify_web_assets_ui.py"],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=SUITES, nargs="?", default="smoke")
    parser.add_argument("--artifacts", default="work/verification/logs")
    args = parser.parse_args()
    tools = Path(__file__).resolve().parent
    artifacts = Path(args.artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    failed = []
    for script in SUITES[args.suite]:
        result = subprocess.run(
            [sys.executable, str(tools / script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        (artifacts / f"{Path(script).stem}.log").write_text(result.stdout, encoding="utf-8")
        print(result.stdout, end="")
        if result.returncode:
            failed.append(script)
    if failed:
        print("Failed UI checks: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
