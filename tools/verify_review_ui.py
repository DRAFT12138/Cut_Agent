"""Exercise persistent finishing checks and their export in a real browser."""
from __future__ import annotations

from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from uuid import uuid4
import json
import socket
import time
from urllib.parse import quote
from zipfile import ZipFile

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
            run, _ = runs.start_run(str(ROOT / "sample_media"), copy, seed=23,
                                    options={"preview": False, "finishing_llm": False, "platform": "douyin"},
                                    sync=True)
            run_id = run.run_id
            assert runs.status_of(run_id)["status"] == "done"
            plan_path = runctl.run_dir(run_id) / "plan.json"
            original_plan = plan_path.read_bytes()
            calls_before_review = model_calls

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
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                review_statuses = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("response", lambda response: review_statuses.append(response.status)
                        if response.request.method == "PUT" and response.url.endswith(f"/api/runs/{run_id}/review") else None)

                def open_guide(tab):
                    tab.goto(base, wait_until="networkidle")
                    tab.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
                    tab.get_by_role("tab", name="精剪指导").click()
                    tab.locator(".ant-tabs-tabpane-active input[type=checkbox]").first.wait_for(timeout=15000)

                open_guide(page)
                check = page.locator(".ant-tabs-tabpane-active input[type=checkbox]").first
                page.get_by_text("已核对 0/", exact=False).wait_for(timeout=15000)
                assert not check.is_checked()
                check.check()
                page.get_by_text("已核对 1/", exact=False).wait_for(timeout=15000)
                assert check.is_checked()
                saved = runctl.read_json(runctl.run_dir(run_id) / "review.json")
                assert saved["checked"][0] is True
                assert plan_path.read_bytes() == original_plan, "checking modified the plan"
                page.locator(".ant-tabs-tabpane-active .ant-card").last.screenshot(path=str(root / "ui-review-checked.png"))

                open_guide(page)
                page.get_by_text("已核对 1/", exact=False).wait_for(timeout=15000)
                assert page.locator(".ant-tabs-tabpane-active input[type=checkbox]").first.is_checked()
                guide_art = page.request.get(f"{base}/api/runs/{run_id}/stages/finishing_guide").json()
                first_text = guide_art["finishing"]["checklist"][0]["text"]
                original_guide = page.request.get(f"{base}/api/file?path={quote(guide_art['guide_path'])}&run={run_id}")
                assert f"- [ ] {first_text}" in original_guide.text()
                exported = page.request.get(f"{base}/api/runs/{run_id}/review.md")
                assert exported.status == 200
                assert f"- [x] {first_text}" in exported.text()
                assert "已核对 1/" in exported.text()
                with page.expect_download() as download_info:
                    page.get_by_role("link", name="下载人工核对记录.md").click()
                download = download_info.value
                assert download.suggested_filename == "精剪核对记录.md"
                assert f"- [x] {first_text}" in Path(download.path()).read_text(encoding="utf-8")
                with page.expect_download() as package_info:
                    page.get_by_role("link", name="下载精剪交接包.zip").click()
                package = package_info.value
                assert package.suggested_filename == "精剪交接包.zip"
                with ZipFile(package.path()) as bundle:
                    members = set(bundle.namelist())
                    assert {"交接说明.md", "精剪指导.md", "精剪核对记录.md", "粗剪方案.md",
                            "plan.json", "cut_lines.txt", "frames/storyboard_0001.png"} <= members
                    assert f"- [x] {first_text}" in bundle.read("精剪核对记录.md").decode()
                    assert bundle.read("plan.json") == original_plan
                    assert all(not member.startswith("../") for member in members)

                edit = page.request.post(f"{base}/api/runs/{run_id}/edit",
                                         data={"op": "duration", "row": 1, "seconds": 7.5,
                                               "expected_revision": 0})
                assert edit.status == 200, edit.text()
                page.get_by_text("指导内容已变化，原核对状态未沿用，请重新检查。").wait_for(timeout=30000)
                page.get_by_text("已核对 0/", exact=False).wait_for(timeout=30000)
                assert not page.locator(".ant-tabs-tabpane-active input[type=checkbox]").first.is_checked()
                after = page.request.get(f"{base}/api/runs/{run_id}/review").json()
                assert after["plan_revision"] == 1 and after["reset_for_new_guide"] is True
                assert not any(item["checked"] for item in after["items"])
                assert f"- [ ] {first_text}" in page.request.get(f"{base}/api/runs/{run_id}/review.md").text()
                refreshed_package = page.request.get(f"{base}/api/runs/{run_id}/handoff.zip")
                assert refreshed_package.status == 200
                with ZipFile(BytesIO(refreshed_package.body())) as bundle:
                    assert f"- [ ] {first_text}" in bundle.read("精剪核对记录.md").decode()
                    assert bundle.read("plan.json") == plan_path.read_bytes()
                page.locator(".ant-tabs-tabpane-active .ant-card").last.screenshot(path=str(root / "ui-review-reset.png"))

                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.on("pageerror", lambda error: errors.append(str(error)))
                mobile.goto(base, wait_until="networkidle")
                mobile.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
                mobile.locator(".run-stage-picker .ant-select").click()
                mobile.locator(".ant-select-dropdown:visible .ant-select-item-option").last.click()
                mobile.locator(".ant-select-dropdown").wait_for(state="hidden", timeout=12000)
                mobile.get_by_text("已核对 0/", exact=False).wait_for(timeout=15000)
                mobile_width = mobile.evaluate("document.documentElement.scrollWidth")
                assert mobile_width <= 390
                mobile.locator(".ant-tabs-tabpane-active .ant-card").last.screenshot(path=str(root / "ui-review-mobile.png"))
                assert review_statuses == [200], review_statuses
                assert model_calls == calls_before_review
                assert not errors, errors

                report = {"run_id": run_id, "checklist_items": len(after["items"]),
                          "persisted_after_reopen": True, "original_guide_unchanged": True,
                          "checked_export_status": exported.status,
                          "browser_download_filename": download.suggested_filename,
                          "handoff_download_filename": package.suggested_filename,
                          "handoff_members": sorted(members), "handoff_reset_after_edit": True,
                          "reset_after_edit": True,
                          "final_plan_revision": after["plan_revision"], "review_put_statuses": review_statuses,
                          "review_and_edit_model_calls": model_calls - calls_before_review,
                          "mobile_document_width": mobile_width, "browser_errors": errors}
                path = root / "review-ui-verification.json"
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
