"""Verify that two open Web editors converge after a real edit in either tab."""
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
    model_calls = 0

    def chat(system, user, **kwargs):
        nonlocal model_calls
        model_calls += 1
        if "分镜师" in system:
            return {"segments": [{"text": text, "duration": 8, "role": role, "intensity": intensity}
                    for text, role, intensity in zip(texts, ["开头", "高潮", "收尾"], [2, 5, 2])]}
        if "时间线编排" in system:
            return {"timeline": [{"media": name, "shot_idx": 1, "segment_text": text}
                    for name, text in zip(["city_night.mp4", "mountain_day.mp4", "ocean_waves.mp4"], texts)]}
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
            run, _ = runs.start_run(str(ROOT / "sample_media"), copy, seed=21,
                                    options={"preview": False, "finishing_llm": False, "platform": "douyin"},
                                    sync=True)
            run_id = run.run_id
            assert runs.status_of(run_id)["status"] == "done"
            original = runctl.read_json(runctl.run_dir(run_id) / "plan.json")
            assert len(original["timeline"]) == 3
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
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                a, b = context.new_page(), context.new_page()
                errors = []
                edit_statuses = []
                for page in (a, b):
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("response", lambda response: edit_statuses.append(response.status)
                            if response.request.method == "POST" and response.url.split("?", 1)[0].endswith("/edit") else None)
                    page.goto(base, wait_until="networkidle")
                    page.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
                    page.get_by_role("tab", name="2 · 调整镜头").click()
                    page.get_by_label("第 1 行秒数").wait_for(timeout=15000)
                    assert float(page.get_by_label("第 1 行秒数").input_value()) == 8

                a.get_by_label("第 1 行秒数").fill("7.5")
                a.get_by_label("第 1 行秒数").press("Tab")
                a.get_by_text("计划已保存并重建").wait_for(timeout=30000)
                b.wait_for_function("() => Number(document.querySelector('input[aria-label=\"第 1 行秒数\"]')?.value) === 7.5", timeout=30000)
                b.get_by_text("计划已在其他页面更新至修订 1").wait_for(timeout=12000)
                assert float(b.get_by_label("第 2 行秒数").input_value()) == 8
                b.locator("#plan-editor").screenshot(path=str(root / "ui-second-tab-refreshed.png"))

                b.get_by_label("第 2 行秒数").fill("7.5")
                b.get_by_label("第 2 行秒数").press("Tab")
                b.get_by_text("计划已保存并重建").wait_for(timeout=30000)
                a.wait_for_function("() => Number(document.querySelector('input[aria-label=\"第 2 行秒数\"]')?.value) === 7.5", timeout=30000)
                a.get_by_text("计划已在其他页面更新至修订 2").wait_for(timeout=12000)
                final = a.request.get(f"{base}/api/runs/{run_id}/plan").json()
                assert final["revision"] == 2
                assert [row["use_duration"] for row in final["timeline"]] == [7.5, 7.5, 8]
                assert final["storyboard"]["cards"][2]["timeline_in_frame"] == 375
                assert final["storyboard"]["cards"][2]["timeline_in_tc"] == "00:00:15:00"
                assert final["finishing"]["steps"][2]["anchor"] == final["storyboard"]["cards"][2]
                assert edit_statuses == [200, 200], edit_statuses
                assert model_calls == calls_before_edit
                assert not errors, errors
                a.locator("#plan-editor").screenshot(path=str(root / "ui-first-tab-refreshed.png"))
                report = {"run_id": run_id, "initial_revision": original.get("revision", 0),
                          "final_revision": final["revision"], "durations": [row["use_duration"] for row in final["timeline"]],
                          "second_tab_refreshed_before_edit": True, "first_tab_refreshed_after_edit": True,
                          "edit_statuses": edit_statuses, "edit_model_calls": model_calls - calls_before_edit,
                          "browser_errors": errors}
                path = root / "cross-tab-edit-ui-verification.json"
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
