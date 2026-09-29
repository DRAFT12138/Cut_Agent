"""Exercise the live Web stage workflow with real media and controlled offline model replies.

The run root is isolated under work/verification. Browser actions use the actual
FastAPI endpoints; only remote model/music/browser dependencies are replaced.
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from threading import Event, Thread
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


def wait_status(request, base: str, run_id: str, status: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = request.get(f"{base}/api/runs/{run_id}").json()
        if meta["status"] == status:
            return meta
        if meta["status"] == "failed":
            raise AssertionError(meta)
        time.sleep(.2)
    raise AssertionError(f"run did not reach {status}: {meta}")


def wait_revision(request, base: str, run_id: str, revision: int, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        plan = request.get(f"{base}/api/runs/{run_id}/plan").json()
        if plan.get("revision", 0) == revision:
            return plan
        time.sleep(.2)
    raise AssertionError(f"plan did not reach revision {revision}")


def main() -> None:
    root = Path("work/verification") / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:4])
    root.mkdir(parents=True)
    runctl.set_runs_root_for_test((root / "runs").resolve())
    copy = (ROOT / "sample_copy.txt").read_text(encoding="utf-8")
    texts = [copy[i:i + (len(copy) + 2) // 3] for i in range(0, len(copy), (len(copy) + 2) // 3)]
    first_model_entered = Event()
    release_first_model = Event()
    timeline_entered = Event()
    release_timeline = Event()
    first_call = True
    model_calls = 0

    def chat(system, user, **kwargs):
        nonlocal first_call, model_calls
        model_calls += 1
        if first_call:
            first_call = False
            first_model_entered.set()
            assert release_first_model.wait(60), "first model gate timed out"
        if "分镜师" in system:
            return {"segments": [{"text": t, "duration": 8, "role": role, "intensity": strength}
                    for t, role, strength in zip(texts, ["开头", "高潮", "收尾"], [2, 5, 2])]}
        if "时间线编排" in system:
            return {"timeline": [{"media": media, "shot_idx": 1, "segment_text": t}
                    for media, t in zip(["city_night.mp4", "mountain_day.mp4", "ocean_waves.mp4"], texts)]}
        return {"mood": "离线验证", "primary": {}, "alternatives": []}

    original_stages = runs.STAGES
    def held_timeline(state):
        timeline_entered.set()
        assert release_timeline.wait(60), "timeline gate timed out"
        return graph.build_timeline(state)

    stages = [(name, held_timeline if name == "build_timeline" else fn)
              for name, fn in original_stages]
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
            stack.enter_context(patch.object(runs, "STAGES", stages))
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
                edits = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: edits.append(request.post_data_json)
                        if request.method == "POST" and request.url.split("?", 1)[0].endswith("/edit") else None)
                page.goto(base, wait_until="networkidle")
                page.get_by_role("button", name="新建粗剪任务").click()
                page.locator("#media_dir").fill(str((ROOT / "sample_media").resolve()))
                page.locator("#copy").fill(copy)
                page.locator(".ant-modal-footer button.ant-btn-primary").click()
                assert first_model_entered.wait(60), "run did not reach plan_segments"
                run_id = page.locator("code").first.inner_text()
                page.get_by_text("当前流程节点：第 2/8 阶段 · 文案分段").wait_for(timeout=10000)
                assert page.get_by_role("tab", name="文案分段").get_attribute("aria-selected") == "true"
                overview = page.get_by_role("region", name="粗剪流程阶段")
                assert overview.locator(".run-phase").count() == 4
                assert overview.locator(".run-stage-node").count() == 8
                planning = overview.locator('[data-stage="plan_segments"]')
                assert "进行中" in planning.inner_text() and "旁白段落" in planning.inner_text()
                assert "is-current" in planning.get_attribute("class")
                assert planning.get_attribute("aria-pressed") == "true"
                overview.locator('[data-stage="scan_media"]').click()
                page.get_by_text("当前查看：扫描素材 · 已完成").wait_for(timeout=10000)
                assert page.get_by_text("当前流程节点：第 2/8 阶段 · 文案分段").is_visible()
                planning.click()
                assert planning.get_attribute("aria-pressed") == "true"
                page.screenshot(path=str(root / "ui-live-plan-running.png"), full_page=False)

                page.locator("button").filter(has_text="暂停").first.click()
                page.get_by_text("暂停请求已收到，当前子任务结束后生效。").wait_for(timeout=10000)
                pending = page.request.get(f"{base}/api/runs/{run_id}").json()
                assert pending["status"] == "running" and pending["control_requested"] == "pause", pending
                page.screenshot(path=str(root / "ui-live-pause-requested.png"), full_page=False)
                release_first_model.set()
                paused = wait_status(page.request, base, run_id, "paused")
                page.locator("button").filter(has_text="恢复").first.wait_for(timeout=10000)
                # Pause is cooperative: the in-flight model unit commits, then
                # the next stage becomes the resumable checkpoint.
                assert paused["resumable_from"] == "understand_media", paused
                assert paused["stages"][1]["status"] == "done", paused
                page.get_by_text("任务已暂停").wait_for(timeout=10000)
                assert page.get_by_text("任务已暂停").count() == 1, "stage events were duplicated"
                page.screenshot(path=str(root / "ui-live-paused.png"), full_page=False)
                page.get_by_role("tab", name="文案分段").click()
                plan_arc = page.get_by_role("img", name="文案分段强度（初稿），强度 1 至 5：2、5、2")
                plan_arc.wait_for(timeout=12000)
                assert page.get_by_text("强度 5/5").count() == 1
                page.screenshot(path=str(root / "ui-plan-intensity-arc.png"), full_page=False)
                page.get_by_role("button", name="回到当前阶段").click()

                page.locator("button").filter(has_text="恢复").first.click()
                assert timeline_entered.wait(60), "resumed run did not reach timeline"
                page.get_by_text("当前流程节点：第 4/8 阶段 · 时间线编排").wait_for(timeout=10000)
                assert page.get_by_role("tab", name="时间线编排").get_attribute("aria-selected") == "true"
                assert "is-current" in overview.locator('[data-stage="build_timeline"]').get_attribute("class")
                page.screenshot(path=str(root / "ui-live-timeline-running.png"), full_page=False)
                release_timeline.set()
                completed = wait_status(page.request, base, run_id, "done", 120)
                page.get_by_text("流程已完成：8/8 阶段").wait_for(timeout=12000)
                page.get_by_text("当前查看：时间线编排 · 已完成").wait_for(timeout=12000)
                page.get_by_role("tab", name="时间线编排").wait_for(timeout=12000)
                assert page.get_by_role("tab", name="时间线编排").get_attribute("aria-selected") == "true"
                page.get_by_role("button", name="调整此行").first.wait_for(timeout=12000)
                original_plan = page.request.get(f"{base}/api/runs/{run_id}/plan").json()
                page.get_by_role("tab", name="3 · 查看自检").click()
                expected_arc = "、".join(f"{value:g}" for value in original_plan["critique"]["intensity_arc"])
                page.get_by_role("img", name=f"当前时间线强度（自检后），强度 1 至 5：{expected_arc}").wait_for(timeout=12000)
                page.get_by_text("自动修复或手动移行后，曲线顺序可能与文案分段初稿不同", exact=False).wait_for(timeout=12000)
                page.screenshot(path=str(root / "ui-critique-intensity-arc.png"), full_page=False)
                page.get_by_role("tab", name="1 · 看分镜").click()
                page.get_by_role("button", name="调整此行").first.click()
                page.get_by_role("tab", name="2 · 调整镜头").wait_for(timeout=12000)
                assert page.get_by_role("tab", name="2 · 调整镜头").get_attribute("aria-selected") == "true"
                assert all(not card["frame_missing"] for card in original_plan["storyboard"]["cards"])
                page.wait_for_function("() => [...document.querySelectorAll('.ant-tabs-tabpane-active img')].filter(image => image.complete && image.naturalWidth > 0).length >= 3", timeout=12000)
                original_doc_version = completed["artifact_versions"]["write_doc"]
                model_calls_before_edit = model_calls
                events = page.request.get(f"{base}/api/runs/{run_id}/events").json()["events"]
                assert any(e.get("what") == "run-paused" for e in events)
                assert any(e.get("what") == "run-done" for e in events)
                assert not errors, errors
                page.screenshot(path=str(root / "ui-live-done.png"), full_page=False)

                new_duration = round(original_plan["timeline"][0]["use_duration"] - .5, 2)
                assert new_duration > 0
                duration_input = page.get_by_label("第 1 行秒数")
                duration_input.fill(str(new_duration))
                duration_input.press("Tab")
                page.get_by_text("计划已保存并重建").wait_for(timeout=120000)
                edited = page.request.get(f"{base}/api/runs/{run_id}/plan").json()
                assert edited["revision"] == original_plan.get("revision", 0) + 1, edited["revision"]
                assert abs(edited["timeline"][0]["use_duration"] - new_duration) < .001
                assert model_calls == model_calls_before_edit, "offline edit unexpectedly called model"
                page.wait_for_function("duration => document.querySelector('.ant-tabs-tabpane-active')?.innerText.includes(`${duration.toFixed(1)}s`)", arg=new_duration, timeout=12000)
                refreshed = page.request.get(f"{base}/api/runs/{run_id}").json()
                assert refreshed["artifact_versions"]["write_doc"] != original_doc_version
                doc = page.request.get(f"{base}/api/runs/{run_id}/stages/write_doc").json()
                assert doc["revision"] == edited["revision"]
                assert doc["storyboard"]["cards"][0]["source_out_tc"] != original_plan["storyboard"]["cards"][0]["source_out_tc"]
                page.get_by_role("tab", name="文档产出").click()
                page.locator(".ant-tabs-tabpane-active").get_by_text(doc["storyboard"]["cards"][0]["source_out_tc"], exact=False).first.wait_for(timeout=12000)
                page.screenshot(path=str(root / "ui-live-edited-document.png"), full_page=False)

                page.get_by_role("tab", name="时间线编排").click()
                candidate = edited["timeline"][0]["alternatives"][0]
                page.locator(".plan-editor__row").first.locator(".plan-editor__candidate").first.click()
                swapped = wait_revision(page.request, base, run_id, 2)
                assert swapped["timeline"][0]["media"] == candidate["media"]
                assert swapped["timeline"][0]["shot_idx"] == candidate["shot_idx"]
                page.locator("#plan-editor").get_by_text(f"#1 · {candidate['media']}").wait_for(timeout=12000)
                page.screenshot(path=str(root / "ui-live-swapped.png"), full_page=False)

                before_move = [(row["media"], row["segment_text"]) for row in swapped["timeline"]]
                page.locator(".plan-editor__row").first.locator(".ant-select").nth(1).click()
                page.locator(".ant-select-dropdown:visible .ant-select-item-option").first.click()
                moved = wait_revision(page.request, base, run_id, 3)
                after_move = [(row["media"], row["segment_text"]) for row in moved["timeline"]]
                assert after_move[:2] == [before_move[1], before_move[0]], (before_move, after_move, edits)
                assert [operation["op"] for operation in edits] == ["duration", "swap", "move"], edits
                assert edits[-1]["row"] == 1 and edits[-1]["to"] == 2, edits[-1]
                page.locator("#plan-editor").get_by_text(f"#2 · {candidate['media']}").wait_for(timeout=12000)
                assert model_calls == model_calls_before_edit, "visual edits unexpectedly called model"
                final_doc = page.request.get(f"{base}/api/runs/{run_id}/stages/write_doc").json()
                final_guide = page.request.get(f"{base}/api/runs/{run_id}/stages/finishing_guide").json()
                assert final_doc["revision"] == moved["revision"]
                assert [card["media"] for card in final_doc["storyboard"]["cards"]] == [row["media"] for row in moved["timeline"]]
                assert [step["anchor"] for step in final_guide["finishing"]["steps"]] == final_doc["storyboard"]["cards"]
                page.screenshot(path=str(root / "ui-live-reordered.png"), full_page=False)
                drag_source = page.locator("#edit-row-3 .plan-editor__drag-handle")
                drag_target = page.locator("#edit-row-1")
                # drag_to pre-scrolls the distant target and can leave the source
                # outside the viewport before mouse-down. Follow a user's actual
                # pointer path, scrolling the page while the drag is held.
                drag_source.wait_for(timeout=12000)
                page.wait_for_function("() => document.querySelector('#edit-row-3 .plan-editor__drag-handle')?.draggable === true")
                drag_source.scroll_into_view_if_needed()
                drag_scroll_start = page.evaluate("window.scrollY")
                source_box = drag_source.bounding_box()
                assert source_box
                drag_x = source_box["x"] + source_box["width"] / 2
                drag_y = source_box["y"] + source_box["height"] / 2
                page.mouse.move(drag_x, drag_y)
                page.mouse.down()
                page.mouse.move(drag_x + 12, drag_y - 30, steps=6)
                page.mouse.move(drag_x + 12, 28, steps=18)
                page.mouse.wheel(0, -620)
                page.wait_for_timeout(350)
                drag_scroll_end = page.evaluate("window.scrollY")
                assert drag_scroll_end < drag_scroll_start, (drag_scroll_start, drag_scroll_end)
                target_box = drag_target.bounding_box()
                assert target_box
                page.mouse.move(target_box["x"] + target_box["width"] / 2,
                                target_box["y"] + target_box["height"] / 2, steps=18)
                page.mouse.up()
                dragged = wait_revision(page.request, base, run_id, 4)
                expected_drag_order = [after_move[2], after_move[0], after_move[1]]
                assert [(row["media"], row["segment_text"]) for row in dragged["timeline"]] == expected_drag_order
                assert edits[-1]["op"] == "move" and edits[-1]["row"] == 3 and edits[-1]["to"] == 1, edits[-1]
                drag_doc = page.request.get(f"{base}/api/runs/{run_id}/stages/write_doc").json()
                drag_guide = page.request.get(f"{base}/api/runs/{run_id}/stages/finishing_guide").json()
                assert [card["media"] for card in drag_doc["storyboard"]["cards"]] == [row["media"] for row in dragged["timeline"]]
                assert [step["anchor"] for step in drag_guide["finishing"]["steps"]] == drag_doc["storyboard"]["cards"]
                page.locator("#plan-editor").get_by_text(f"#1 · {dragged['timeline'][0]['media']}").wait_for(timeout=12000)
                page.screenshot(path=str(root / "ui-live-dragged.png"), full_page=False)
                assert model_calls == model_calls_before_edit, "drag unexpectedly called model"
                assert not errors, errors
                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile_errors = []
                mobile.on("pageerror", lambda error: mobile_errors.append(str(error)))
                mobile.goto(base, wait_until="networkidle")
                mobile.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
                mobile.get_by_role("combobox", name="查看阶段").wait_for(timeout=12000)
                assert not mobile.locator(".run-phase-overview").is_visible()
                mobile.get_by_role("button", name="查看完整流程").click()
                mobile_overview = mobile.get_by_role("region", name="粗剪流程阶段")
                assert mobile_overview.locator(".run-phase").count() == 4
                mobile_overview.locator('[data-stage="finishing_guide"]').click()
                mobile.get_by_text("当前查看：精剪指导 · 已完成").wait_for(timeout=12000)
                assert mobile_overview.locator('[data-stage="finishing_guide"]').get_attribute("aria-pressed") == "true"
                mobile.screenshot(path=str(root / "ui-mobile-stages.png"), full_page=False)
                mobile.get_by_role("button", name="收起完整流程").click()
                assert not mobile_overview.is_visible()
                assert mobile.evaluate("document.documentElement.scrollWidth") <= 390
                assert not mobile_errors, mobile_errors
                mobile.close()
                report = {"run_id": run_id, "run_status": completed["status"],
                          "paused_at": paused["resumable_from"], "observed_stages": [2, 4, 8],
                          "pause_request_visible": True, "resume_via_ui": True,
                          "stage_events_deduplicated": True,
                          "plan_intensity_arc": [2, 5, 2],
                          "critique_intensity_arc": original_plan["critique"]["intensity_arc"],
                          "phase_groups": 4, "phase_nodes": 8, "mobile_flow_expandable": True,
                          "completed_artifacts": len(completed["has_artifacts"]),
                          "visual_storyboard_frames": len(original_plan["storyboard"]["cards"]),
                          "edit_revision": edited["revision"], "first_row_seconds": new_duration,
                          "document_version_refreshed": True, "model_calls_during_edit": model_calls - model_calls_before_edit,
                          "swap_revision": swapped["revision"], "swapped_media": candidate["media"],
                          "move_revision": moved["revision"], "move_via": "target-row-select",
                          "drag_revision": dragged["revision"], "drag_via": "cross-viewport-handle",
                          "drag_scroll": [drag_scroll_start, drag_scroll_end],
                          "order_after_drag": [row["media"] for row in dragged["timeline"]],
                          "submitted_edits": edits,
                          "order_after_move": [row["media"] for row in moved["timeline"]],
                          "roles_after_move": [row["role"] for row in moved["timeline"]],
                          "browser_errors": errors, "model": "offline fixed replies",
                          "media": str((ROOT / "sample_media").resolve())}
                path = root / "live-stage-ui-verification.json"
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(path.resolve())
                browser.close()
    finally:
        release_first_model.set()
        release_timeline.set()
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            server_thread.join(10)
        runctl.set_runs_root_for_test(None)


if __name__ == "__main__":
    main()
