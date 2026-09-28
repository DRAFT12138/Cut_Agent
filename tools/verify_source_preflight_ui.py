"""Check source handoff warnings in a real finished run and Chrome page."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from uuid import uuid4
import json
import os
import shutil
import socket
import time

from playwright.sync_api import sync_playwright
import uvicorn

from cut_agent import graph, runs, runctl, vision
from cut_agent.config import ROOT
from cut_agent.llm import LLMError
from cut_agent.server import make_app


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def main() -> None:
    root = (Path("work/verification") / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:4])).resolve()
    root.mkdir(parents=True)
    media_dir = root / "media"
    shutil.copytree(ROOT / "sample_media", media_dir)
    runctl.set_runs_root_for_test(root / "runs")
    copy = (ROOT / "sample_copy.txt").read_text(encoding="utf-8")
    width = (len(copy) + 2) // 3
    texts = [copy[i : i + width] for i in range(0, len(copy), width)]

    def chat(system, user, **kwargs):
        if "分镜师" in system:
            return {
                "segments": [
                    {"text": text, "duration": 8, "role": role, "intensity": intensity}
                    for text, role, intensity in zip(texts, ["开头", "高潮", "收尾"], [2, 5, 2])
                ]
            }
        if "时间线编排" in system:
            return {
                "timeline": [
                    {"media": name, "shot_idx": 1, "segment_text": text}
                    for name, text in zip(["city_night.mp4", "mountain_day.mp4", "ocean_waves.mp4"], texts)
                ]
            }
        return {"mood": "离线验证", "primary": {}, "alternatives": []}

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    server = None
    server_thread = None
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(graph, "chat_json", chat))
            stack.enter_context(patch.object(graph, "VISION_ENABLED", True))
            stack.enter_context(patch.object(vision, "chat_vision", side_effect=LLMError("offline check")))
            stack.enter_context(patch.object(graph, "download_for_mood", return_value=[]))
            stack.enter_context(patch.object(graph, "controller", side_effect=RuntimeError("offline check")))
            run, _ = runs.start_run(
                str(media_dir), copy, seed=23, options={"preview": True, "finishing_llm": False}, sync=True
            )
            run_id = run.run_id
            assert runs.status_of(run_id)["status"] == "done"
            assert runctl.read_json(runctl.run_dir(run_id) / "plan.json")["preview"]["status"] == "ready"
            server = uvicorn.Server(uvicorn.Config(make_app(), host="127.0.0.1", port=port, log_level="error"))
            server_thread = Thread(target=server.run, daemon=True)
            server_thread.start()
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True, executable_path=CHROME, args=["--disable-gpu", "--no-sandbox"]
                )
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base, wait_until="networkidle")
                page.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
                page.get_by_role("tab", name="精剪指导").click()
                page.screenshot(path=str(root / "ui-before-source-check.png"), full_page=True)
                page.get_by_text("画面文件可找到 3/3", exact=False).wait_for(timeout=15000)
                page.get_by_text("需复核画面 0 行", exact=False).wait_for()
                original = page.request.get(f"{base}/api/runs/{run_id}/sources").json()
                assert len(original["pictures"]) == 3
                assert all(row["snapshot"] == "match" for row in original["pictures"])
                page.locator(".ant-tabs-tabpane-active .ant-card").filter(has_text="交接前素材核对").last.screenshot(
                    path=str(root / "ui-source-preflight-ready.png")
                )

                changed_path = Path(original["pictures"][0]["path"])
                assert changed_path.is_relative_to(media_dir)
                stat = changed_path.stat()
                os.utime(changed_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
                page.get_by_role("button", name="重新核对").click()
                page.get_by_text("需复核画面 1 行", exact=False).wait_for(timeout=15000)
                page.get_by_text("与任务输入快照不同，需复核", exact=False).wait_for()
                current = page.request.get(f"{base}/api/runs/{run_id}/sources").json()
                assert current["pictures"][0]["snapshot"] == "changed"
                assert all(row["snapshot"] == "match" for row in current["pictures"][1:])
                manifest = page.request.get(f"{base}/api/runs/{run_id}/sources.md").text()
                assert " | 已变化，需复核 | " in manifest
                page.locator(".ant-tabs-tabpane-active .ant-card").filter(has_text="交接前素材核对").last.screenshot(
                    path=str(root / "ui-source-preflight-changed.png")
                )
                page.locator(".ant-tabs-tabpane-active .ant-card").filter(has_text="交接前素材核对").last.get_by_role(
                    "button", name="调整第 1 行"
                ).click()
                page.locator(".ant-tabs-tabpane-active #edit-row-1").wait_for(timeout=15000)
                page.get_by_role("button", name="返回预览").click()
                page.locator(".ant-tabs-tabpane-active #preview-player video").wait_for(timeout=15000)
                page.locator(".ant-tabs-tabpane-active .preview-timeline__segment").nth(1).click()
                page.get_by_role("button", name="调整当前第 2 行").wait_for()
                page.locator("#preview-player").screenshot(path=str(root / "ui-preview-edit-entry.png"))
                page.get_by_role("button", name="调整当前第 2 行").click()
                page.locator(".ant-tabs-tabpane-active #edit-row-2").wait_for(timeout=15000)
                page.locator(".ant-tabs-tabpane-active #edit-row-2").screenshot(
                    path=str(root / "ui-preview-focused-row.png")
                )
                page.get_by_role("tab", name="精剪指导").click()
                page.locator(".ant-tabs-tabpane-active").get_by_role("button", name="调整第 3 行").click()
                page.locator(".ant-tabs-tabpane-active #edit-row-3").wait_for(timeout=15000)

                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.on("pageerror", lambda error: errors.append(str(error)))
                mobile.goto(base, wait_until="networkidle")
                mobile.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
                mobile.locator(".run-stage-picker .ant-select").click()
                mobile.locator(".ant-select-dropdown:visible .ant-select-item-option").last.click()
                mobile.get_by_text("需复核画面 1 行", exact=False).wait_for(timeout=15000)
                mobile_width = mobile.evaluate("document.documentElement.scrollWidth")
                assert mobile_width <= 390, mobile_width
                mobile.locator(".ant-tabs-tabpane-active .ant-card").filter(has_text="交接前素材核对").last.screenshot(
                    path=str(root / "ui-source-preflight-mobile.png")
                )
                mobile.locator(".ant-tabs-tabpane-active .ant-card").filter(has_text="交接前素材核对").last.get_by_role(
                    "button", name="调整第 1 行"
                ).click()
                mobile.locator(".ant-tabs-tabpane-active #edit-row-1").wait_for(timeout=15000)
                mobile.get_by_role("button", name="返回预览").click()
                mobile.locator(".ant-tabs-tabpane-active #preview-player video").wait_for(timeout=15000)
                assert mobile.evaluate("document.documentElement.scrollWidth") <= 390
                assert not errors, errors
                report = {
                    "run_id": run_id,
                    "revision": current["revision"],
                    "picture_rows": len(current["pictures"]),
                    "changed_row": 1,
                    "manifest_agrees": True,
                    "preview_to_editor_row": 2,
                    "guide_to_editor_row": 3,
                    "issue_to_editor_row": 1,
                    "editor_returns_to_preview": True,
                    "mobile_width": mobile_width,
                    "browser_errors": errors,
                }
                result = root / "source-preflight-ui-verification.json"
                result.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(result)
                browser.close()
    finally:
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            server_thread.join(10)
        runctl.set_runs_root_for_test(None)


if __name__ == "__main__":
    main()
