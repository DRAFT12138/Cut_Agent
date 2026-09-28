"""Open an extracted finishing handoff with file://, without a Cut Agent server."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile
import json
import time

from playwright.sync_api import sync_playwright

from cut_agent import editing, graph, handoff, runs, runctl, vision
from cut_agent.config import ROOT
from cut_agent.llm import LLMError


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

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(graph, "chat_json", chat))
            stack.enter_context(patch.object(graph, "VISION_ENABLED", True))
            stack.enter_context(patch.object(vision, "chat_vision", side_effect=LLMError("offline check")))
            stack.enter_context(patch.object(graph, "download_for_mood", return_value=[]))
            stack.enter_context(patch.object(graph, "controller", side_effect=RuntimeError("offline check")))
            handle, _ = runs.start_run(str(ROOT / "sample_media"), copy, seed=65,
                                       options={"preview": True, "finishing_llm": False}, sync=True)
            run_id = handle.run_id
            assert runs.status_of(run_id)["status"] == "done"
            plan_path = runctl.run_dir(run_id) / "plan.json"
            draft = runctl.read_json(plan_path)
            draft["timeline"][0]["segment_text"] = "第一句\n第二句\t下一格"
            draft["timeline"][0]["note"] = "复核源片\r\n确认动作"
            runctl.write_json_atomic(plan_path, draft)
            updated = editing.rebuild(run_id, expected_revision=0, preview=True)
            assert updated["revision"] == 1
            archive = handoff.build(run_id)
            portable = root / "portable"
            portable.mkdir()
            with archive, ZipFile(archive) as bundle:
                names = bundle.namelist()
                assert all(".." not in Path(name).parts and not Path(name).is_absolute() for name in names)
                bundle.extractall(portable)
            assert (portable / "精剪交接.html").is_file()
            assert (portable / "素材交接清单.md").is_file()
            assert (portable / "preview.mp4").is_file()
            plan = json.loads((portable / "plan.json").read_text(encoding="utf-8"))
            assert plan["revision"] == updated["revision"]
            source_manifest = (portable / "素材交接清单.md").read_text(encoding="utf-8")
            assert f"方案修订：{plan['revision']}" in source_manifest
            assert all(str(row["media"]) in source_manifest for row in plan["timeline"])
            assert source_manifest.count(" | 一致 | ") == len(plan["timeline"])
            cut_lines = (portable / "cut_lines.txt").read_text(encoding="utf-8").splitlines()
            assert len(cut_lines) == len(plan["timeline"]) + 1
            columns = cut_lines[0].split("\t")
            first_row = cut_lines[1].split("\t")
            assert first_row[5] == "第一句 第二句 下一格"
            assert first_row[6] == "复核源片 确认动作"
            for line, card in zip(cut_lines[1:], plan["storyboard"]["cards"], strict=True):
                values = line.split("\t")
                assert len(values) == len(columns)
                assert values[8:12] == [card["source_in_tc"], card["source_out_tc"],
                                        card["timeline_in_tc"], card["timeline_out_tc"]]

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=CHROME,
                                                 args=["--disable-gpu", "--no-sandbox"])
            errors: list[str] = []
            network_requests: list[str] = []
            uri = (portable / "精剪交接.html").resolve().as_uri()
            desktop = browser.new_page(viewport={"width": 1440, "height": 900})
            desktop.on("pageerror", lambda error: errors.append(str(error)))
            desktop.on("request", lambda request: network_requests.append(request.url)
                       if request.url.startswith(("http:", "https:")) else None)
            desktop.goto(uri, wait_until="load")
            desktop.wait_for_function("() => [...document.querySelectorAll('.document img')].length >= 6 && [...document.querySelectorAll('.document img')].every(img => img.complete && img.naturalWidth > 0)", timeout=15000)
            video = desktop.locator("video")
            desktop.wait_for_function("() => { const v = document.querySelector('video'); return v && Number.isFinite(v.duration) && v.duration > 20 && v.videoWidth > 0; }", timeout=15000)
            video.evaluate("async element => { element.muted = true; await element.play(); }")
            desktop.wait_for_function("() => document.querySelector('video')?.ended", timeout=35000)
            played_to = round(video.evaluate("element => element.currentTime"), 2)
            desktop.screenshot(path=str(root / "offline-handoff-desktop.png"), full_page=False)
            nav = desktop.get_by_role("navigation", name="交接目录")
            nav.get_by_role("link", name="素材交接清单", exact=True).click()
            assert "#cut-sources" in desktop.url
            assert desktop.locator("#cut-sources table").count() >= 1
            desktop.wait_for_function("() => { const top = document.querySelector('#cut-sources')?.getBoundingClientRect().top; return top !== undefined && top >= -5 && top <= 120; }", timeout=10000)
            source_rows = desktop.locator("#cut-sources table").first.locator("tbody tr").count()
            assert source_rows == len(plan["timeline"])
            desktop.screenshot(path=str(root / "offline-handoff-sources.png"), full_page=False)
            nav.get_by_role("link", name="执行卡").click()
            assert "#cut-plan-section-3" in desktop.url
            desktop.get_by_role("heading", name="执行卡", exact=True).last.scroll_into_view_if_needed()
            desktop.screenshot(path=str(root / "offline-handoff-execution-card.png"), full_page=False)
            assert desktop.evaluate("document.documentElement.scrollWidth") <= 1440

            mobile = browser.new_page(viewport={"width": 390, "height": 844})
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            mobile.on("request", lambda request: network_requests.append(request.url)
                      if request.url.startswith(("http:", "https:")) else None)
            mobile.goto(uri, wait_until="load")
            mobile.get_by_text("查看交接目录").click()
            mobile_nav = mobile.get_by_role("navigation", name="移动端交接目录")
            first = mobile_nav.locator("a").nth(0).bounding_box()
            second = mobile_nav.locator("a").nth(1).bounding_box()
            assert first and second and second["y"] >= first["y"] + first["height"]
            mobile.screenshot(path=str(root / "offline-handoff-mobile-directory.png"), full_page=False)
            mobile_nav.get_by_role("link", name="精剪指导", exact=True).click()
            assert "#cut-guide" in mobile.url
            mobile.wait_for_function("() => { const section = document.querySelector('#cut-guide'); const top = section?.getBoundingClientRect().top; return top !== undefined && top >= -5 && top <= 120; }", timeout=10000)
            assert mobile.evaluate("document.documentElement.scrollWidth") <= 390
            mobile.screenshot(path=str(root / "offline-handoff-mobile.png"), full_page=False)
            assert not errors, errors
            assert not network_requests, network_requests
            report = {"run": run_id, "plan_revision": plan["revision"],
                      "portable_html": str((portable / "精剪交接.html").resolve()),
                      "zip_members": len(names), "loaded_images": desktop.locator(".document img").count(),
                      "cut_line_rows": len(cut_lines) - 1, "cut_line_columns": len(columns),
                      "source_manifest_rows": source_rows,
                      "video_duration": round(video.evaluate("element => element.duration"), 2),
                      "video_played_to": played_to,
                      "desktop_execution_card_anchor": "cut-plan-section-3",
                      "mobile_guide_anchor": "cut-guide",
                      "desktop_width": desktop.evaluate("document.documentElement.scrollWidth"),
                      "mobile_width": mobile.evaluate("document.documentElement.scrollWidth"),
                      "browser_errors": errors, "network_requests": network_requests}
            path = root / "offline-handoff-ui-verification.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(path.resolve())
            browser.close()
    finally:
        runctl.set_runs_root_for_test(None)


if __name__ == "__main__":
    main()
