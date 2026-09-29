"""Verify a real parent/variant pair through the Web A/B flow with offline model replies."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from uuid import uuid4
import json
import socket
import time
from zipfile import ZipFile

from playwright.sync_api import sync_playwright
import uvicorn

from cut_agent import graph, runs, runctl, vision
from cut_agent.config import ROOT
from cut_agent.llm import LLMError
from cut_agent.server import make_app


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def wait_done(request, base: str, run_id: str, timeout: float = 150) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = request.get(f"{base}/api/runs/{run_id}")
        assert response.status == 200, f"run status HTTP {response.status}: {response.text()}"
        meta = response.json()
        if meta["status"] == "done":
            return meta
        if meta["status"] == "failed":
            raise AssertionError(meta)
        time.sleep(.25)
    raise AssertionError(f"variant did not complete: {meta}")


def main() -> None:
    root = Path("work/verification") / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:4])
    root.mkdir(parents=True)
    runctl.set_runs_root_for_test((root / "runs").resolve())
    copy = (ROOT / "sample_copy.txt").read_text(encoding="utf-8")
    width = (len(copy) + 2) // 3
    texts = [copy[i:i + width] for i in range(0, len(copy), width)]

    def chat(system, user, **kwargs):
        if "分镜师" in system:
            return {"segments": [{"text": t, "duration": 8, "role": role, "intensity": strength}
                    for t, role, strength in zip(texts, ["开头", "高潮", "收尾"], [2, 5, 2])]}
        if "时间线编排" in system:
            names = (["city_night.mp4", "mountain_day.mp4", "ocean_waves.mp4"]
                     if kwargs.get("seed") == 8 else
                     ["mountain_day.mp4", "ocean_waves.mp4", "city_night.mp4"])
            return {"timeline": [{"media": name, "shot_idx": 1, "segment_text": t}
                    for name, t in zip(names, texts)]}
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
            parent, _ = runs.start_run(str(ROOT / "sample_media"), copy, seed=8,
                                       options={"preview": True, "finishing_llm": False, "platform": "douyin"},
                                       sync=True)
            parent_id = parent.run_id
            assert runs.status_of(parent_id)["status"] == "done"
            server = uvicorn.Server(uvicorn.Config(make_app(), host="127.0.0.1", port=port, log_level="error"))
            server_thread = Thread(target=server.run, daemon=True)
            server_thread.start()
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                time.sleep(.05)
            assert server.started, "local Web server did not start"

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=CHROME,
                                                     args=["--disable-gpu", "--no-sandbox"])
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base, wait_until="networkidle")
                parent_row = page.get_by_role("row").filter(has_text=parent_id)
                parent_row.locator("button").nth(1).click()
                page.wait_for_function("parent => { const codes = [...document.querySelectorAll('.ant-layout-content code')]; return codes.length === 1 && codes[0].textContent !== parent; }", arg=parent_id, timeout=30000)
                variant_id = page.locator("code").first.inner_text()
                assert variant_id != parent_id
                variant_meta = wait_done(page.request, base, variant_id)
                parent_meta = page.request.get(f"{base}/api/runs/{parent_id}").json()
                a_input = runctl.read_json(runctl.run_dir(parent_id) / "input.json")
                b_input = runctl.read_json(runctl.run_dir(variant_id) / "input.json")
                assert a_input["copy"] == b_input["copy"]
                assert a_input["media_snapshot"] == b_input["media_snapshot"]
                assert b_input["options"]["parent_run"] == parent_id
                assert b_input["options"]["comparison_group"] == parent_id
                assert [a_input["seed"], b_input["seed"]] == [8, 9]
                a_plan = page.request.get(f"{base}/api/runs/{parent_id}/plan").json()
                b_plan = page.request.get(f"{base}/api/runs/{variant_id}/plan").json()
                assert [row["media"] for row in a_plan["timeline"]] != [row["media"] for row in b_plan["timeline"]]
                changed_rows = sum(
                    left.get("media") != right.get("media") or left.get("shot_idx") != right.get("shot_idx")
                    or abs(left.get("start_offset", 0) - right.get("start_offset", 0)) > .001
                    or abs(left["use_duration"] - right["use_duration"]) > .001
                    or left.get("segment_text", "") != right.get("segment_text", "")
                    for left, right in zip(a_plan["timeline"], b_plan["timeline"])
                ) + abs(len(a_plan["timeline"]) - len(b_plan["timeline"]))
                assert changed_rows > 0
                assert all(plan["preview"]["status"] == "ready" for plan in (a_plan, b_plan))
                assert len(parent_meta["has_artifacts"]) == len(variant_meta["has_artifacts"]) == 8

                extra_response = page.request.post(f"{base}/api/runs/{parent_id}/variant",
                                                   data=json.dumps({"seed": 10}),
                                                   headers={"Content-Type": "application/json"})
                assert extra_response.status == 200
                extra_id = extra_response.json()["run_id"]
                wait_done(page.request, base, extra_id)
                page.get_by_role("combobox", name="选择对比版本").wait_for(timeout=20000)
                desktop_target = page.get_by_role("combobox", name="选择对比版本").locator("xpath=../..")
                assert parent_id in desktop_target.inner_text(), "variant should compare with parent by default"
                desktop_target.click()
                page.locator(".ant-select-item-option").filter(has_text=extra_id).click()
                assert extra_id in desktop_target.inner_text()
                desktop_target.click()
                page.locator(".ant-select-item-option").filter(has_text=parent_id).click()
                page.keyboard.press("Escape")
                page.locator(".ant-select-dropdown:visible").wait_for(state="hidden", timeout=12000)
                page.screenshot(path=str(root / "ui-variant-compare-entry.png"), full_page=False)
                page.get_by_role("button", name="对比另一版").wait_for(timeout=20000)
                page.get_by_role("button", name="对比另一版").click()
                page.get_by_text("同一 A/B 组").wait_for(timeout=12000)
                page.get_by_text(f"{changed_rows} 行有差异").wait_for(timeout=12000)
                assert page.get_by_role("button", name="返回任务").is_visible()
                page.screenshot(path=str(root / "ui-true-ab-direct.png"), full_page=False)
                with page.expect_download() as package_info:
                    page.get_by_role("link", name="下载 B 版交接包").click()
                with ZipFile(package_info.value.path()) as bundle:
                    assert json.loads(bundle.read("plan.json")) == a_plan
                page.get_by_role("button", name="打开 B 版继续").click()
                assert page.locator("code").first.inner_text() == parent_id
                page.get_by_role("button", name="返回对照").click()
                page.get_by_text("同一 A/B 组").wait_for(timeout=12000)
                page.get_by_role("button", name="返回任务").click()
                page.get_by_text(variant_id).first.wait_for(timeout=12000)
                page.locator(".ant-layout-content button").first.click()
                for run_id in (parent_id, variant_id):
                    page.get_by_role("row").filter(has=page.locator("code").filter(has_text=run_id)).locator("input[type=checkbox]").check()
                page.get_by_role("button", name="A/B 并排对照").click()
                page.get_by_text("同一 A/B 组").wait_for(timeout=12000)
                page.get_by_text(f"{changed_rows} 行有差异").wait_for(timeout=12000)
                seed_tag = page.get_by_text("seed A", exact=False).first.inner_text()
                assert seed_tag in ("seed A 8 / B 9", "seed A 9 / B 8"), seed_tag
                count = page.locator('video[aria-label$="版粗剪预览"]').count()
                assert count == 2
                page.wait_for_function("() => [...document.querySelectorAll('video[aria-label$=\"版粗剪预览\"]')].every(video => Number.isFinite(video.duration) && video.duration > 0)", timeout=30000)
                preview_durations = page.locator('video[aria-label$="版粗剪预览"]').evaluate_all("videos => videos.map(video => video.duration)")
                page.screenshot(path=str(root / "ui-true-ab.png"), full_page=False)
                page.get_by_role("button", name="打开 A 版继续").click()
                assert page.locator("code").first.inner_text() == parent_id
                page.get_by_role("button", name="返回对照").click()
                page.get_by_text("同一 A/B 组").wait_for(timeout=12000)
                assert not errors, errors

                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile_errors = []
                mobile.on("pageerror", lambda error: mobile_errors.append(str(error)))
                mobile.goto(base, wait_until="networkidle")
                mobile.get_by_role("row").filter(has_text=variant_id).locator("button").first.click()
                mobile.get_by_role("combobox", name="选择对比版本").wait_for(timeout=20000)
                mobile_target = mobile.get_by_role("combobox", name="选择对比版本").locator("xpath=../..")
                assert parent_id in mobile_target.inner_text()
                mobile_target.click()
                mobile.locator(".ant-select-item-option").filter(has_text=extra_id).click()
                assert extra_id in mobile_target.inner_text()
                mobile_target.click()
                mobile.locator(".ant-select-item-option").filter(has_text=parent_id).click()
                mobile.keyboard.press("Escape")
                mobile.locator(".ant-select-dropdown:visible").wait_for(state="hidden", timeout=12000)
                mobile.screenshot(path=str(root / "ui-variant-compare-entry-mobile.png"), full_page=False)
                mobile.get_by_role("button", name="对比另一版").wait_for(timeout=20000)
                mobile.get_by_role("button", name="对比另一版").click()
                mobile.get_by_text("同一 A/B 组").wait_for(timeout=12000)
                mobile.get_by_text(f"{changed_rows} 行有差异").wait_for(timeout=12000)
                direct_mobile_width = mobile.evaluate("document.documentElement.scrollWidth")
                assert direct_mobile_width <= 390, f"mobile direct comparison overflows: {direct_mobile_width}px"
                mobile.screenshot(path=str(root / "ui-true-ab-direct-mobile.png"), full_page=False)
                mobile.get_by_role("button", name="打开 A 版继续").click()
                assert mobile.locator("code").first.inner_text() == variant_id
                mobile.get_by_role("button", name="返回对照").click()
                mobile.get_by_text("同一 A/B 组").wait_for(timeout=12000)
                mobile.get_by_role("button", name="返回任务").click()
                mobile.get_by_text(variant_id).first.wait_for(timeout=12000)
                mobile.locator(".ant-layout-content button").first.click()
                for run_id in (parent_id, variant_id):
                    mobile.get_by_role("row").filter(has=mobile.locator("code").filter(has_text=run_id)).locator("input[type=checkbox]").check()
                mobile.get_by_role("button", name="A/B 并排对照").click()
                mobile.get_by_text("同一 A/B 组").wait_for(timeout=12000)
                width = mobile.evaluate("document.documentElement.scrollWidth")
                assert width <= 390, f"mobile comparison overflows: {width}px"
                mobile.screenshot(path=str(root / "ui-true-ab-mobile.png"), full_page=False)
                assert not mobile_errors, mobile_errors
                assert page.request.get(f"{base}/api/runs/{parent_id}/plan").json() == a_plan
                assert page.request.get(f"{base}/api/runs/{variant_id}/plan").json() == b_plan
                report = {"parent_run": parent_id, "variant_run": variant_id,
                          "third_run": extra_id, "multi_version_target_selected": parent_id,
                          "seed_pair": [8, 9], "media_snapshot_equal": True,
                          "parent_recorded": True, "comparison_group": parent_id,
                          "direct_compare_from_variant": True,
                          "open_either_version_and_return": True,
                          "list_compare_open_and_return": True,
                          "handoff_download_from_comparison": True,
                          "parent_order": [row["media"] for row in a_plan["timeline"]],
                          "variant_order": [row["media"] for row in b_plan["timeline"]],
                          "changed_rows": changed_rows, "preview_players": count,
                          "preview_durations": preview_durations, "mobile_document_width": width,
                          "direct_mobile_document_width": direct_mobile_width,
                          "browser_errors": errors + mobile_errors, "plans_unchanged": True,
                          "model": "offline fixed replies"}
                path = root / "true-ab-ui-verification.json"
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
