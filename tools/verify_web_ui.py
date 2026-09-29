"""Headless Chrome check of the stage-oriented Web workflow (requires a running server)."""
from argparse import ArgumentParser
from copy import deepcopy
from pathlib import Path
import json
import time
from uuid import uuid4
from playwright.sync_api import sync_playwright

parser = ArgumentParser()
parser.add_argument("run_id", help="A completed run with at least two preview markers and one alternative shot")
parser.add_argument("--base-url", default="http://127.0.0.1:8090")
parser.add_argument("--chrome", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
parser.add_argument("--compare-run", help="A second completed run on the first list page for A/B UI verification")
args = parser.parse_args()
root = Path("work/verification") / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:4])
root.mkdir(parents=True)
run_id = args.run_id
base = args.base_url.rstrip("/")
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True,
        executable_path=args.chrome,
        args=["--disable-gpu", "--no-sandbox"])
    page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    run_meta = page.request.get(f"{base}/api/runs/{run_id}").json()
    assert run_meta["status"] == "done", "verification requires a completed run"
    stage_art = page.request.get(f"{base}/api/runs/{run_id}/stages/write_doc").json()
    markers = stage_art.get("preview", {}).get("markers", [])
    assert len(markers) >= 2, "verification requires a preview with two segments"
    page.goto(base, wait_until="networkidle", timeout=30000)
    active_stage = page.locator(".run-stage-tabs > .ant-tabs-content-holder > .ant-tabs-content > .ant-tabs-tabpane-active")
    page.screenshot(path=str(root / "ui-list.png"), full_page=True)
    page.get_by_text(run_id).first.wait_for(timeout=15000)
    page.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
    page.get_by_text("流程已完成：").first.wait_for(timeout=15000)
    page.get_by_role("tab", name="时间线编排").wait_for(timeout=15000)
    page.screenshot(path=str(root / "ui-stage-default.png"), full_page=True)
    page.get_by_role("tab", name="3 · 查看自检").click()
    page.get_by_text("剪辑自检").first.wait_for(timeout=12000)
    page.locator(".timeline-workspace").screenshot(path=str(root / "ui-timeline-self-check.png"))
    page.get_by_role("tab", name="1 · 看分镜").click()
    page.get_by_role("button", name="调整此行").first.wait_for(timeout=15000)
    page.get_by_role("button", name="调整此行").first.click()
    assert page.get_by_role("tab", name="2 · 调整镜头").get_attribute("aria-selected") == "true"
    page.locator("#edit-row-1").wait_for(timeout=15000)
    page.screenshot(path=str(root / "ui-editor.png"), full_page=True)
    page.locator("#plan-editor").screenshot(path=str(root / "ui-editor-panel.png"))
    page.get_by_role("button", name="下一步：查看自检").click()
    assert page.get_by_role("tab", name="3 · 查看自检").get_attribute("aria-selected") == "true"
    page.get_by_role("button", name="下一步：检查预览").click()
    assert page.get_by_role("tab", name="文档产出").get_attribute("aria-selected") == "true"
    page.locator("#preview-player").wait_for(timeout=12000)
    assert page.evaluate("() => !!(document.querySelector('#preview-player').compareDocumentPosition(document.querySelector('#rough-cut-document')) & Node.DOCUMENT_POSITION_FOLLOWING)")
    preview_box = page.locator("#preview-player").bounding_box()
    assert preview_box and 0 <= preview_box["y"] < 900, preview_box
    page.screenshot(path=str(root / "ui-preview-entry.png"), full_page=False)
    page.get_by_role("group", name="预览段落导航").screenshot(path=str(root / "ui-preview-timeline.png"))
    page.locator(".preview-timeline__segment").nth(1).click()
    page.wait_for_timeout(300)
    assert page.locator(".preview-timeline__segment.is-active").inner_text().startswith("#2")
    seek_seconds = page.locator("video").evaluate("video => video.currentTime")
    assert abs(seek_seconds - markers[1]["start"]) < .2
    video = page.locator(".ant-tabs-tabpane-active video").first
    page.wait_for_function("() => { const v = document.querySelector('.ant-tabs-tabpane-active video'); return v && Number.isFinite(v.duration) && v.duration > 0 && v.videoWidth > 0 && v.videoHeight > 0; }", timeout=15000)
    video.evaluate("async video => { video.muted = true; video.currentTime = 0; await video.play(); }")
    playback_boundaries = []
    for marker in markers[1:]:
        page.wait_for_function("marker => { const v = document.querySelector('.ant-tabs-tabpane-active video'); const active = document.querySelector('.preview-timeline__segment.is-active span'); return v && !v.paused && !v.error && v.readyState >= 2 && v.currentTime >= marker.start + Math.min(.2, (marker.end - marker.start) / 2) && active?.textContent?.trim() === '#' + marker.seq; }", arg=marker, timeout=max(45000, int((marker["start"] + 5) * 1000)))
        playback_boundaries.append(round(video.evaluate("video => video.currentTime"), 2))
        if len(playback_boundaries) == 1:
            page.locator("#preview-player").screenshot(path=str(root / "ui-preview-playing-second-segment.png"))
        elif len(playback_boundaries) == 2:
            page.locator("#preview-player").screenshot(path=str(root / "ui-preview-playing-third-segment.png"))
    playback_dimensions = video.evaluate("video => ({ width: video.videoWidth, height: video.videoHeight, duration: video.duration })")
    page.wait_for_function("lastSeq => { const v = document.querySelector('.ant-tabs-tabpane-active video'); return v && v.ended && !v.error && Math.abs(v.currentTime - v.duration) < .1 && document.querySelector('.preview-timeline__segment.is-active span')?.textContent?.trim() === '#' + lastSeq; }", arg=markers[-1]["seq"], timeout=max(45000, int((markers[-1]["end"] + 5) * 1000)))
    playback_ended_at = round(video.evaluate("video => video.currentTime"), 2)
    page.get_by_role("button", name="下一步：精剪指导").click()
    assert page.get_by_role("tab", name="精剪指导").get_attribute("aria-selected") == "true"
    page.get_by_role("button", name="返回预览").click()
    assert page.get_by_role("tab", name="文档产出").get_attribute("aria-selected") == "true"
    page.get_by_role("button", name="返回时间线调整").click()
    assert page.get_by_role("tab", name="时间线编排").get_attribute("aria-selected") == "true"
    page.get_by_role("tab", name="2 · 调整镜头").click()
    plan = page.request.get(f"{base}/api/runs/{run_id}/plan").json()
    edits = []
    def handle_edit(route):
        edits.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(plan))
    page.route("**/api/runs/*/edit", handle_edit)
    page.locator(".plan-editor__candidate").first.click()
    page.wait_for_timeout(300)
    assert edits and edits[0]["op"] == "swap"
    drag_source = page.locator(".plan-editor__drag-handle").first
    drag_target = page.locator(".plan-editor__row").nth(1)
    drag_target.scroll_into_view_if_needed()
    drag_source.drag_to(drag_target, steps=10)
    page.wait_for_timeout(300)
    assert any(edit["op"] == "move" and edit["row"] == 1 and edit["to"] == 2 for edit in edits), edits
    duration = page.get_by_label("第 1 行秒数")
    new_duration = round(plan["timeline"][0]["use_duration"] + .5, 2)
    duration.fill(str(new_duration))
    duration.press("Tab")
    page.wait_for_timeout(300)
    assert any(edit["op"] == "duration" and edit["seconds"] == new_duration for edit in edits)
    for label in ("扫描素材", "文案分段", "视觉理解", "时间线编排", "网络素材", "配乐", "文档产出", "精剪指导"):
        page.get_by_role("tab", name=label).click()
        assert active_stage.is_visible(), label
    guide_art = page.request.get(f"{base}/api/runs/{run_id}/stages/finishing_guide").json()
    guide = guide_art["finishing"]
    guide_text = active_stage.inner_text()
    assert len(guide["steps"]) == len(plan["timeline"])
    for step in guide["steps"]:
        anchor = step["anchor"]
        assert anchor["timeline_in_tc"] in guide_text and anchor["timeline_out_tc"] in guide_text
        assert anchor["source_in_tc"] in guide_text and anchor["source_out_tc"] in guide_text
        assert (anchor.get("media") or "待补素材") in guide_text
        narration = " ".join(str(anchor.get("text") or "").split())
        if narration:
            assert narration[:10] in guide_text
    checklist_count = active_stage.locator("input[type=checkbox]").count()
    assert checklist_count == len(guide["checklist"])
    for item in guide["checklist"]:
        assert item["text"] in guide_text
    # This viewer check stays read-only; verify_review_ui.py exercises real
    # persisted checkbox writes on its own isolated run.
    cover_count = len(guide["covers"])
    page.wait_for_function("count => [...document.querySelectorAll('.run-stage-tabs > .ant-tabs-content-holder > .ant-tabs-content > .ant-tabs-tabpane-active .ant-image-img')].filter(image => image.complete && image.naturalWidth > 0).length === count", arg=cover_count + len(guide["steps"]), timeout=12000)
    guide_link = page.get_by_role("link", name="导出原始精剪指导.md").get_attribute("href")
    guide_file = page.request.get(f"{base}{guide_link}")
    assert guide_file.status == 200
    guide_markdown = guide_file.text()
    assert all(step["anchor"]["timeline_in_tc"] in guide_markdown for step in guide["steps"])
    assert "[查看粗剪方案与素材目录](粗剪方案.md)" in guide_markdown
    for step in guide["steps"]:
        anchor = step["anchor"]
        shot = f" #{anchor['shot_idx']}" if anchor["shot_idx"] is not None else ""
        assert f"- 取材：{anchor.get('media') or '待补素材'}{shot}" in guide_markdown
        assert (" ".join(str(anchor.get("text") or "").split()) or "无旁白画面") in guide_markdown
    page.wait_for_function("() => document.querySelectorAll('.ant-message-notice').length === 0", timeout=12000)
    active_stage.screenshot(path=str(root / "ui-finishing-guide.png"))
    assert not errors, errors
    assert page.request.get(f"{base}/api/runs/{run_id}/plan").json() == plan, "UI check changed the saved plan"
    running_meta = deepcopy(run_meta)
    running_meta["status"] = "running"
    running_meta["has_artifacts"] = {stage["name"]: i < 3 for i, stage in enumerate(running_meta["stages"])}
    for i, stage in enumerate(running_meta["stages"]):
        stage["status"] = "done" if i < 3 else "running" if i == 3 else "pending"
        stage["progress"] = {"done": 1, "total": 3, "item": "mountain_day.mp4"} if i == 3 else None
    running = browser.new_page(viewport={"width": 1440, "height": 900})
    running_errors = []
    running.on("pageerror", lambda error: running_errors.append(str(error)))
    running.route(f"**/api/runs/{run_id}", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(running_meta)))
    running.goto(base, wait_until="networkidle")
    running.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
    running.get_by_text("当前流程节点：第 4/8 阶段 · 时间线编排").wait_for()
    running.get_by_text("正在处理 mountain_day.mp4").wait_for()
    assert running.get_by_role("tab", name="时间线编排").get_attribute("aria-selected") == "true"
    running.screenshot(path=str(root / "ui-running-stage.png"), full_page=False)
    assert not running_errors, running_errors
    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    mobile_errors = []
    mobile.on("pageerror", lambda error: mobile_errors.append(str(error)))
    mobile.goto(base, wait_until="networkidle")
    mobile.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
    mobile.get_by_role("tab", name="2 · 调整镜头").click()
    mobile.locator("#plan-editor").wait_for()
    viewport_width = mobile.evaluate("innerWidth")
    document_width = mobile.evaluate("document.documentElement.scrollWidth")
    assert document_width <= viewport_width, f"mobile page overflows: {document_width}px > {viewport_width}px"
    mobile.screenshot(path=str(root / "ui-mobile-stage.png"), full_page=False)
    mobile.locator("#plan-editor").screenshot(path=str(root / "ui-mobile-editor.png"))
    mobile.get_by_role("button", name="下一步：查看自检").click()
    mobile.get_by_role("button", name="下一步：检查预览").click()
    mobile.locator("#preview-player").wait_for(timeout=12000)
    mobile_preview_box = mobile.locator("#preview-player").bounding_box()
    assert mobile_preview_box and 0 <= mobile_preview_box["y"] < 844, mobile_preview_box
    mobile.screenshot(path=str(root / "ui-mobile-preview-entry.png"), full_page=False)
    assert mobile.evaluate("document.documentElement.scrollWidth") <= viewport_width
    assert not mobile.locator(".run-stage-tabs > .ant-tabs-nav").is_visible()
    mobile.locator(".run-stage-picker .ant-select").click()
    mobile.locator(".ant-select-dropdown:visible .ant-select-item-option").last.click()
    assert "精剪指导" in mobile.locator(".run-stage-picker .ant-select-selection-item").inner_text()
    mobile.locator(".ant-select-dropdown").wait_for(state="hidden", timeout=12000)
    mobile.locator(".run-stage-tabs > .ant-tabs-content-holder > .ant-tabs-content > .ant-tabs-tabpane-active .ant-image-img").first.wait_for(timeout=12000)
    mobile_guide_width = mobile.evaluate("document.documentElement.scrollWidth")
    assert mobile_guide_width <= viewport_width, f"mobile finishing guide overflows: {mobile_guide_width}px"
    mobile.screenshot(path=str(root / "ui-mobile-finishing-guide.png"), full_page=False)
    assert not mobile_errors, mobile_errors
    comparison = None
    compare_errors = []
    if args.compare_run:
        compare = browser.new_page(viewport={"width": 1440, "height": 900})
        compare.on("pageerror", lambda error: compare_errors.append(str(error)))
        compare.goto(base, wait_until="networkidle")
        for compare_id in (run_id, args.compare_run):
            compare.get_by_role("row").filter(has_text=compare_id).locator("input[type=checkbox]").check()
        compare.get_by_role("button", name="A/B 并排对照").click()
        compare.get_by_text("两版差异").wait_for()
        compare.get_by_text("独立任务对照").wait_for()
        second_plan = compare.request.get(f"{base}/api/runs/{args.compare_run}/plan").json()
        expected_changes = sum(
            left.get("media") != right.get("media") or left.get("shot_idx") != right.get("shot_idx")
            or abs(left.get("start_offset", 0) - right.get("start_offset", 0)) > .001
            or abs(left["use_duration"] - right["use_duration"]) > .001
            or left.get("segment_text", "") != right.get("segment_text", "")
            for left, right in zip(plan["timeline"], second_plan["timeline"])
        ) + abs(len(plan["timeline"]) - len(second_plan["timeline"]))
        compare.get_by_text(f"{expected_changes} 行有差异").wait_for()
        preview_count = compare.locator('video[aria-label$="版粗剪预览"]').count()
        assert preview_count == sum(p.get("preview", {}).get("status") == "ready" for p in (plan, second_plan))
        compare.screenshot(path=str(root / "ui-comparison.png"), full_page=False)
        assert not compare_errors, compare_errors
        narrow_compare = browser.new_page(viewport={"width": 390, "height": 844})
        narrow_compare.goto(base, wait_until="networkidle")
        for compare_id in (run_id, args.compare_run):
            narrow_compare.get_by_role("row").filter(has_text=compare_id).locator("input[type=checkbox]").check()
        narrow_compare.get_by_role("button", name="A/B 并排对照").click()
        narrow_compare.get_by_text("两版差异").wait_for()
        compare_width = narrow_compare.evaluate("document.documentElement.scrollWidth")
        assert compare_width <= 390, f"mobile comparison overflows: {compare_width}px"
        narrow_compare.screenshot(path=str(root / "ui-mobile-comparison.png"), full_page=False)
        comparison = {"second_run": args.compare_run, "changed_rows": expected_changes,
                      "preview_players": preview_count, "mobile_document_width": compare_width}
    report = {"run_id": run_id, "browser_errors": errors + running_errors + mobile_errors + compare_errors,
              "completed_run_default_stage": "build_timeline", "stage_tabs_checked": 8,
              "timeline_substeps_checked": 3,
              "guided_stage_handoffs_checked": 5, "preview_before_document": True,
              "mobile_preview_entry_visible": True,
              "editor_rows": len(plan["timeline"]), "preview_segments": len(markers),
              "preview_seek_seconds": seek_seconds, "preview_playback_boundaries": playback_boundaries,
              "preview_decoded_video": playback_dimensions, "preview_playback_ended_at": playback_ended_at,
              "intercepted_edits": edits,
              "finishing_steps": len(guide["steps"]),
              "finishing_storyboard_images_loaded": len(guide["steps"]),
              "finishing_covers_loaded": cover_count,
              "finishing_checklist_items": checklist_count,
              "guide_download_status": guide_file.status,
              "running_stage": "build_timeline", "running_progress": "1/3 mountain_day.mp4",
              "mobile_viewport_width": viewport_width, "mobile_document_width": document_width,
              "mobile_finishing_document_width": mobile_guide_width,
              "comparison": comparison,
              "real_plan_edited": False}
    report_path = root / "web-ui-verification.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report_path.resolve())
    browser.close()
