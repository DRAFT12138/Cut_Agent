"""Verify an existing delivery through the real FastAPI routes (no browser/LLM).

This verifies HTTP contracts and asset availability, not visual layout or UI actions.
Run after tools/verify_delivery.py: uv run python tools/verify_web_exports.py RUN_ID
"""
import argparse
import json
from pathlib import Path
import re

from fastapi.testclient import TestClient

from cut_agent import runctl, runs
from cut_agent.server import make_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    args = parser.parse_args()
    root = runctl.run_dir(args.run_id)
    paths = set()
    with TestClient(make_app()) as client:
        index = client.get("/")
        assert index.status_code == 200
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', index.text)
        assert assets, "Build the Web app first"
        for url in assets:
            assert client.get(url).status_code == 200, url
        meta = client.get(f"/api/runs/{args.run_id}").json()
        assert meta["status"] == "done" and len(meta["artifact_versions"]) == len(runs.STAGES)
        for stage in runs.STAGE_NAMES:
            response = client.get(f"/api/runs/{args.run_id}/stages/{stage}")
            assert response.status_code == 200, stage
        plan = client.get(f"/api/runs/{args.run_id}/plan").json()
        for name in ("plan.md", "粗剪方案.md", "精剪指导.md", "plan.json", "cut_lines.txt"):
            paths.add(root / name)
        for item in plan["media"]:
            paths.update(Path(path) for path in item.get("thumbnails", []))
        for name in ("plan.md", "精剪指导.md"):
            text = (root / name).read_text(encoding="utf-8")
            for image in re.findall(r'!\[[^\]]*\]\(<([^>]+)>\)', text):
                paths.add(root / image)
        for path in sorted(paths):
            response = client.get("/api/file", params={"path": str(path), "run": args.run_id})
            assert response.status_code == 200 and response.content, path
        if (plan.get("preview") or {}).get("status") == "ready":
            preview = client.get(f"/api/runs/{args.run_id}/preview", headers={"Range": "bytes=0-127"})
            assert preview.status_code == 206 and len(preview.content) == 128
    report = {"run_id": args.run_id, "stages": len(runs.STAGES), "static_assets": assets,
              "exported_files_checked": len(paths), "result": "passed",
              "scope": "FastAPI HTTP and files only; browser layout/actions not verified"}
    runctl.write_json_atomic(root / "web-verification.json", report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
