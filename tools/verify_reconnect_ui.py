"""Verify that a run keeps its stage view during API loss and catches up after reconnect."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from uuid import uuid4
import json
import re
import socket
import time

from playwright.sync_api import sync_playwright
import requests
import uvicorn

from cut_agent import graph, runs, runctl, vision
from cut_agent.config import ROOT
from cut_agent.llm import LLMError
from cut_agent.server import make_app


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def main() -> None:
    root = Path("work/verification") / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:4])
    root.mkdir(parents=True)
    runctl.set_runs_root_for_test((root / "runs").resolve())
    copy = (ROOT / "sample_copy.txt").read_text(encoding="utf-8")
    width = (len(copy) + 2) // 3
    texts = [copy[i:i + width] for i in range(0, len(copy), width)]
    model_calls = 0

    def chat(system, user, **kwargs):
        nonlocal model_calls
        model_calls += 1
        if "分镜师" in system:
            return {"segments": [{"text": text, "duration": 8, "role": role, "intensity": intensity}
                    for text, role, intensity in zip(texts, ("开头", "高潮", "收尾"), (2, 5, 2))]}
        if "时间线编排" in system:
            return {"timeline": [{"media": name, "shot_idx": 1, "segment_text": text}
                    for name, text in zip(("city_night.mp4", "mountain_day.mp4", "ocean_waves.mp4"), texts)]}
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
            handle, _ = runs.start_run(str(ROOT / "sample_media"), copy, seed=41,
                                       options={"preview": False, "finishing_llm": False}, sync=True)
            run_id = handle.run_id
            assert runs.status_of(run_id)["status"] == "done"
            calls_before_edit = model_calls

            server = uvicorn.Server(uvicorn.Config(make_app(), host="127.0.0.1", port=port, log_level="error"))
            server_thread = Thread(target=server.run, daemon=True)
            server_thread.start()
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                time.sleep(.05)
            assert server.started

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=CHROME,
                                                     args=["--disable-gpu", "--no-sandbox"])
                errors: list[str] = []
                pattern = re.compile(rf"/api/runs/{re.escape(run_id)}$")
                def abort_metadata(route):
                    route.abort("failed")

                desktop = browser.new_page(viewport={"width": 1440, "height": 900})
                desktop.on("pageerror", lambda error: errors.append(str(error)))
                desktop.goto(base, wait_until="networkidle")
                desktop.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
                desktop.get_by_role("tab", name="2 · 调整镜头").click()
                first_duration = desktop.get_by_label("第 1 行秒数")
                first_duration.wait_for(timeout=15000)
                assert float(first_duration.input_value()) == 8
                desktop.route(pattern, abort_metadata)
                desktop.get_by_text("连接中断，正在自动重试").wait_for(timeout=15000)
                assert float(first_duration.input_value()) == 8
                assert desktop.get_by_role("tab", name="2 · 调整镜头").get_attribute("aria-selected") == "true"
                desktop.screenshot(path=str(root / "run-during-disconnect.png"), full_page=False)

                response = desktop.request.post(f"{base}/api/runs/{run_id}/edit",
                                                data={"op": "duration", "row": 1, "seconds": 7.5,
                                                      "expected_revision": 0})
                assert response.ok, response.text()
                assert response.json()["revision"] == 1
                assert float(first_duration.input_value()) == 8
                desktop.unroute(pattern, abort_metadata)
                desktop.get_by_text("连接中断，正在自动重试").wait_for(state="hidden", timeout=15000)
                desktop.wait_for_function("() => Number(document.querySelector('input[aria-label=\"第 1 行秒数\"]')?.value) === 7.5", timeout=30000)
                assert desktop.get_by_role("tab", name="2 · 调整镜头").get_attribute("aria-selected") == "true"
                desktop.screenshot(path=str(root / "run-after-reconnect.png"), full_page=False)

                # Disable the entire browser network for more than three
                # completed-run polling intervals; the separate Python HTTP
                # client still publishes a new backend revision meanwhile.
                offline_failures: list[str] = []
                desktop.on("requestfailed", lambda request: offline_failures.append(request.url)
                           if pattern.search(request.url) else None)
                desktop.context.set_offline(True)
                offline_started = time.monotonic()
                desktop.get_by_text("连接中断，正在自动重试").wait_for(timeout=15000)
                desktop.wait_for_timeout(27000)
                assert len(offline_failures) >= 3, offline_failures
                assert float(first_duration.input_value()) == 7.5
                assert desktop.get_by_role("tab", name="2 · 调整镜头").get_attribute("aria-selected") == "true"
                desktop.screenshot(path=str(root / "run-after-long-offline.png"), full_page=False)
                changed = requests.post(f"{base}/api/runs/{run_id}/edit",
                                        json={"op": "duration", "row": 1, "seconds": 7,
                                              "expected_revision": 1}, timeout=30)
                assert changed.ok, changed.text
                assert changed.json()["revision"] == 2
                assert float(first_duration.input_value()) == 7.5
                offline_seconds = round(time.monotonic() - offline_started, 1)
                desktop.context.set_offline(False)
                desktop.get_by_text("连接中断，正在自动重试").wait_for(state="hidden", timeout=20000)
                desktop.wait_for_function("() => Number(document.querySelector('input[aria-label=\"第 1 行秒数\"]')?.value) === 7", timeout=30000)
                assert desktop.get_by_role("tab", name="2 · 调整镜头").get_attribute("aria-selected") == "true"
                desktop.screenshot(path=str(root / "run-after-long-offline-recovery.png"), full_page=False)
                desktop.get_by_role("tab", name="文档产出").click()
                desktop.wait_for_function("() => document.querySelector('.plan-document')?.textContent?.includes('画面 7.00s')", timeout=15000)
                desktop.screenshot(path=str(root / "document-after-long-offline.png"), full_page=False)

                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.on("pageerror", lambda error: errors.append(str(error)))
                mobile.route(pattern, abort_metadata)
                mobile.goto(base, wait_until="networkidle")
                mobile.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
                mobile.get_by_text("任务状态暂时无法同步").wait_for(timeout=12000)
                mobile.screenshot(path=str(root / "mobile-initial-disconnect.png"), full_page=False)
                mobile.unroute(pattern, abort_metadata)
                mobile.get_by_role("button", name="立即重试").click()
                mobile.get_by_text("流程已完成：8/8 阶段").wait_for(timeout=15000)
                mobile.locator(".run-stage-picker .ant-select").click()
                mobile.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(has_text="时间线编排").click()
                mobile.locator(".ant-select-dropdown:visible").wait_for(state="hidden", timeout=12000)
                assert mobile.evaluate("document.documentElement.scrollWidth") <= 390
                mobile.screenshot(path=str(root / "mobile-after-reconnect.png"), full_page=False)
                assert not errors, errors
                assert model_calls == calls_before_edit
                report = {"run_id": run_id, "revision_after_reconnect": 2,
                          "stale_duration_during_disconnect": 8, "updated_duration_after_reconnect": 7.5,
                          "full_browser_offline_seconds": offline_seconds,
                          "offline_status_request_failures": len(offline_failures),
                          "stale_duration_during_full_offline": 7.5,
                          "updated_duration_after_full_reconnect": 7,
                          "document_after_full_reconnect": "画面 7.00s",
                          "stage_selection_preserved": True, "desktop_auto_reconnected": True,
                          "mobile_initial_retry_recovered": True,
                          "mobile_width": mobile.evaluate("document.documentElement.scrollWidth"),
                          "edit_model_calls": model_calls - calls_before_edit, "browser_errors": errors}
                path = root / "reconnect-ui-verification.json"
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(path.resolve())
                browser.close()
    finally:
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            server_thread.join(10)
        runctl.set_runs_root_for_test(None)


if __name__ == "__main__":
    main()
