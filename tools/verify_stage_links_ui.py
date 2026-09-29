"""Check run/stage URLs, refresh, history, and comparison return in Chrome."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from uuid import uuid4
import json
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
    root = Path("work/verification") / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:4])
    root.mkdir(parents=True)
    runctl.set_runs_root_for_test((root / "runs").resolve())
    copy = (ROOT / "sample_copy.txt").read_text(encoding="utf-8")
    width = (len(copy) + 2) // 3
    texts = [copy[i:i + width] for i in range(0, len(copy), width)]

    def chat(system, user, **kwargs):
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
            handle, _ = runs.start_run(str(ROOT / "sample_media"), copy, seed=61,
                                       options={"preview": False, "finishing_llm": False}, sync=True)
            first = handle.run_id
            variant, _ = runs.start_variant(first, seed=62, sync=True)
            second = variant.run_id
            run_ids = [first, second]
            assert all(runs.status_of(run_id)["status"] == "done" for run_id in run_ids)
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
                desktop = browser.new_page(viewport={"width": 1440, "height": 900})
                errors: list[str] = []
                desktop.on("pageerror", lambda error: errors.append(str(error)))
                desktop.goto(base, wait_until="networkidle")
                first_row = desktop.get_by_role("row").filter(has=desktop.locator("code").filter(has_text=first))
                first_row.locator("button").first.click()
                assert f"run={first}" in desktop.url
                desktop.get_by_role("tab", name="配乐").click()
                assert "stage=pick_music" in desktop.url
                desktop.reload(wait_until="networkidle")
                desktop.get_by_text("当前查看：配乐", exact=False).wait_for(timeout=12000)
                assert desktop.get_by_role("tab", name="配乐").get_attribute("aria-selected") == "true"
                desktop.screenshot(path=str(root / "stage-after-refresh.png"), full_page=False)

                desktop.get_by_role("tab", name="时间线编排").click()
                desktop.get_by_role("tab", name="2 · 调整镜头").click()
                assert "pane=editor" in desktop.url
                desktop.reload(wait_until="networkidle")
                desktop.get_by_label("第 1 行秒数").wait_for(timeout=15000)
                assert desktop.get_by_role("tab", name="2 · 调整镜头").get_attribute("aria-selected") == "true"
                desktop.screenshot(path=str(root / "editor-after-refresh.png"), full_page=False)

                desktop.go_back()
                first_row.wait_for(timeout=12000)
                assert "run=" not in desktop.url
                desktop.go_forward()
                desktop.get_by_label("第 1 行秒数").wait_for(timeout=15000)
                assert desktop.get_by_role("tab", name="2 · 调整镜头").get_attribute("aria-selected") == "true"

                desktop.get_by_role("tab", name="配乐").click()
                desktop.get_by_role("button", name="对比另一版").click()
                desktop.get_by_text("同一 A/B 组").wait_for(timeout=15000)
                assert "returnStage=pick_music" in desktop.url and "returnPane=editor" in desktop.url
                desktop.get_by_role("button", name="打开 B 版继续").click()
                assert f"run={second}" in desktop.url and f"compare={first}" in desktop.url
                desktop.reload(wait_until="networkidle")
                desktop.get_by_role("button", name="返回对照").wait_for(timeout=15000)
                desktop.get_by_role("button", name="返回对照").click()
                desktop.get_by_text("同一 A/B 组").wait_for(timeout=15000)
                assert f"compare={first}" in desktop.url and f"compare={second}" in desktop.url
                desktop.screenshot(path=str(root / "comparison-after-return.png"), full_page=False)
                desktop.get_by_role("button", name="返回任务").click()
                desktop.get_by_text("当前查看：配乐", exact=False).wait_for(timeout=15000)
                assert "stage=pick_music" in desktop.url and "pane=editor" in desktop.url
                desktop.get_by_role("tab", name="时间线编排").click()
                desktop.get_by_label("第 1 行秒数").wait_for(timeout=15000)
                assert desktop.get_by_role("tab", name="2 · 调整镜头").get_attribute("aria-selected") == "true"
                desktop.screenshot(path=str(root / "task-after-comparison.png"), full_page=False)

                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.on("pageerror", lambda error: errors.append(str(error)))
                mobile.goto(f"{base}?run={first}&stage=finishing_guide", wait_until="networkidle")
                mobile.get_by_text("当前查看：精剪指导", exact=False).wait_for(timeout=15000)
                assert mobile.evaluate("document.documentElement.scrollWidth") <= 390
                mobile.screenshot(path=str(root / "mobile-direct-stage.png"), full_page=False)
                assert not errors, errors
                report = {"runs": run_ids, "stage_refresh": "pick_music", "pane_refresh": "editor",
                          "history_back_forward": True, "comparison_return_after_refresh": True,
                          "comparison_origin_stage_restored": "pick_music",
                          "mobile_direct_stage": "finishing_guide",
                          "mobile_width": mobile.evaluate("document.documentElement.scrollWidth"),
                          "browser_errors": errors}
                path = root / "stage-links-ui-verification.json"
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
