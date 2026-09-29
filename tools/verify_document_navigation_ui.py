"""Check generated plan section navigation in desktop and narrow Chrome."""
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
            handle, _ = runs.start_run(str(ROOT / "sample_media"), copy, seed=63,
                                       options={"preview": False, "finishing_llm": False}, sync=True)
            run_id = handle.run_id
            assert runs.status_of(run_id)["status"] == "done"
            server = uvicorn.Server(uvicorn.Config(make_app(), host="127.0.0.1", port=port, log_level="error"))
            server_thread = Thread(target=server.run, daemon=True)
            server_thread.start()
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                time.sleep(.05)
            assert server.started

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True,
                                                     args=["--disable-gpu", "--no-sandbox"])
                errors: list[str] = []
                findings = {}
                for width in (1440, 390):
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"{base}?run={run_id}&stage=write_doc", wait_until="networkidle")
                    nav = page.get_by_role("navigation", name="文档章节")
                    nav.wait_for(timeout=15000)
                    labels = nav.locator("button").all_inner_texts()
                    assert "分镜总览" in labels and "执行卡" in labels and "自检报告" in labels, labels
                    assert "时间线明细" in labels and "素材利用率" in labels, labels
                    article = page.get_by_role("article", name="方案正文")
                    checks = {}
                    for label in ("分镜总览", "执行卡", "自检报告"):
                        button = nav.get_by_role("button", name=label, exact=True)
                        button.click()
                        page.wait_for_timeout(650)
                        position = article.evaluate("(a, name) => { const h = [...a.querySelectorAll('h2')].find(x => x.textContent.trim() === name); return { scroll_top: a.scrollTop, max_scroll: a.scrollHeight - a.clientHeight, heading_offset: h.getBoundingClientRect().top - a.getBoundingClientRect().top, height: a.clientHeight, current: document.querySelector('.plan-sections button[aria-current=location]')?.textContent }; }", label)
                        assert position["current"] == label, position
                        assert position["scroll_top"] > 0, position
                        assert -2 <= position["heading_offset"] <= position["height"] - 30, position
                        if position["scroll_top"] < position["max_scroll"] - 2:
                            assert position["heading_offset"] <= 90, position
                        checks[label] = {key: round(value) if isinstance(value, float) else value
                                         for key, value in position.items() if key != "current"}
                    page.wait_for_timeout(1100)
                    article.evaluate("element => { element.scrollTop = 0; }")
                    page.wait_for_function("() => !document.querySelector('.plan-sections button[aria-current=location]')")
                    assert page.evaluate("document.documentElement.scrollWidth") <= width
                    page.locator("#rough-cut-document").scroll_into_view_if_needed()
                    page.screenshot(path=str(root / f"document-navigation-{width}.png"), full_page=False)
                    if width == 1440:
                        response = page.request.post(f"{base}/api/runs/{run_id}/edit",
                                                     data={"op": "duration", "row": 1, "seconds": 7.5,
                                                           "expected_revision": 0})
                        assert response.ok, response.text()
                        assert response.json()["revision"] == 1
                        page.wait_for_function("() => document.querySelector('.plan-document')?.textContent?.includes('画面 7.50s')", timeout=30000)
                        assert nav.locator("button").all_inner_texts() == labels
                        nav.get_by_role("button", name="执行卡", exact=True).click()
                        assert page.get_by_role("navigation", name="文档章节").get_by_role("button", name="执行卡", exact=True).get_attribute("aria-current") == "location"
                        findings["revision_after_edit"] = 1
                    findings[str(width)] = {"sections": labels, "jump_scroll_tops": checks,
                                             "page_width": page.evaluate("document.documentElement.scrollWidth")}
                    page.close()
                assert not errors, errors
                report = {"run": run_id, "views": findings, "browser_errors": errors}
                path = root / "document-navigation-ui-verification.json"
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
