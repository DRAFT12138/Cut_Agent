"""Verify real downloaded and failed web assets in the browser's network stage."""
from __future__ import annotations

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from uuid import uuid4
import json
import socket
import time

from PIL import Image
from playwright.sync_api import sync_playwright
import uvicorn

from cut_agent import graph, runs, runctl, vision
from cut_agent.llm import LLMError
from cut_agent.server import make_app


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SEGMENTS = ("需要蓝色远景", "需要人工补画面")


def main() -> None:
    root = Path("work/verification") / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:4])
    root.mkdir(parents=True)
    runctl.set_runs_root_for_test((root / "runs").resolve())
    media_dir = root / "media"
    media_dir.mkdir()
    Image.new("RGB", (640, 480), "red").save(media_dir / "replacement.png")
    encoded = BytesIO()
    Image.new("RGB", (640, 480), "blue").save(encoded, "PNG")
    hits: list[str] = []

    class MediaHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path != "/live.png":
                self.send_response(404)
                self.end_headers()
                return
            payload = encoded.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    media_server = ThreadingHTTPServer(("127.0.0.1", 0), MediaHandler)
    media_thread = Thread(target=media_server.serve_forever, daemon=True)
    media_thread.start()
    media_base = f"http://127.0.0.1:{media_server.server_port}"
    model_calls = 0

    def chat(system, user, **kwargs):
        nonlocal model_calls
        model_calls += 1
        if "分镜师" in system:
            return {"segments": [{"text": text, "duration": 2, "role": role, "intensity": intensity,
                                  "kw_en": ["blue" if i == 0 else "missing"]}
                    for i, (text, role, intensity) in enumerate(zip(SEGMENTS, ("开头", "收尾"), (3, 2)))]}
        if "时间线编排" in system:
            return {"timeline": [{"media": "", "segment_text": text, "use_duration": 2}
                                 for text in SEGMENTS]}
        return {"mood": "离线验证", "primary": {}, "alternatives": []}

    def search(row, query, assets):
        live = row["segment_text"] == SEGMENTS[0]
        assets.append({"name": "蓝色图片" if live else "失效图片", "url": media_base + ("/live.png" if live else "/missing.png"),
                       "from": "本机素材源", "query": query, "for_segment": row["segment_text"]})

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
            stack.enter_context(patch.object(graph, "ows_client", return_value=type("Search", (), {"health": lambda self: True})()))
            stack.enter_context(patch.object(graph, "_explore_via_owsearch", search))
            stack.enter_context(patch.object(graph, "download_for_mood", return_value=[]))
            handle, _ = runs.start_run(str(media_dir.resolve()), "。".join(SEGMENTS),
                                       options={"finishing_llm": False}, sync=True)
            run_id = handle.run_id
            assert runs.status_of(run_id)["status"] == "done"
            artifact = runs.stage_artifact(run_id, "explore_web")
            assets = artifact["web_assets"]
            assert [asset["status"] for asset in assets] == ["verified", "manual"], assets
            assert assets[0]["width"] == 640 and assets[0]["height"] == 480
            assert Path(assets[0]["local"]).is_file() and Path(assets[0]["thumbnail"]).is_file()
            assert "404" in assets[1]["error"] and not assets[1].get("local")
            rows = artifact["timeline"]
            assert any(row.get("asset_status") == "verified" and row.get("ref") == assets[0]["local"] for row in rows)
            assert any(row.get("asset_status") == "manual" and row.get("needs_web") for row in rows)
            assert hits.count("/live.png") >= 2 and "/missing.png" in hits

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
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base, wait_until="networkidle")
                page.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
                page.get_by_role("tab", name="网络素材").click()
                page.get_by_text("已验证入库").first.wait_for(timeout=12000)
                page.get_by_text("待人工替换").first.wait_for(timeout=12000)
                page.get_by_text("当前时间线待替换 1 行").wait_for(timeout=12000)
                page.get_by_text("对应旁白：" + SEGMENTS[0]).wait_for(timeout=12000)
                page.get_by_text("对应旁白：" + SEGMENTS[1]).wait_for(timeout=12000)
                page.get_by_text("图片 · 640×480").wait_for(timeout=12000)
                page.get_by_role("img", name="蓝色图片").wait_for(timeout=12000)
                page.wait_for_function("() => [...document.images].some(image => image.alt === '蓝色图片' && image.complete && image.naturalWidth === 640)")
                assert page.get_by_role("link", name="本机素材源").count() == 2
                assert page.get_by_role("link", name="打开本地素材").count() == 1
                page.locator(".ant-tabs-tabpane-active .ant-typography-danger").filter(has_text="404 Client Error").wait_for(timeout=12000)
                page.screenshot(path=str(root / "ui-web-assets-desktop.png"), full_page=False)
                failed_seq = next(row["seq"] for row in rows if row.get("asset_status") == "manual")
                calls_before_edit = model_calls
                page.get_by_role("button", name=f"去替换第 {failed_seq} 行").click()
                page.get_by_role("tab", name="时间线编排").wait_for(state="visible", timeout=12000)
                assert page.get_by_role("tab", name="时间线编排").get_attribute("aria-selected") == "true"
                page.get_by_role("tab", name="2 · 调整镜头").wait_for(state="visible", timeout=12000)
                assert page.get_by_role("tab", name="2 · 调整镜头").get_attribute("aria-selected") == "true"
                edit_row = page.locator(f"#edit-row-{failed_seq}")
                edit_row.wait_for(state="visible", timeout=12000)
                page.screenshot(path=str(root / "ui-web-assets-edit-entry.png"), full_page=False)
                with page.expect_response(lambda response: response.url.endswith("/edit") and response.request.method == "POST") as moved_response:
                    edit_row.locator("button").first.click()
                assert moved_response.value.ok, moved_response.value.text()
                assert moved_response.value.json()["revision"] == 1
                page.get_by_role("tab", name="网络素材").click()
                failed_seq = 1
                page.get_by_role("button", name=f"去替换第 {failed_seq} 行").wait_for(timeout=12000)
                assert page.get_by_role("button", name="去替换第 2 行").count() == 0
                page.screenshot(path=str(root / "ui-web-assets-after-move.png"), full_page=False)
                page.get_by_role("button", name=f"去替换第 {failed_seq} 行").click()
                edit_row = page.locator(f"#edit-row-{failed_seq}")
                edit_row.wait_for(state="visible", timeout=12000)
                edit_row.locator(".ant-select").first.click()
                with page.expect_response(lambda response: response.url.endswith("/edit") and response.request.method == "POST") as replaced_response:
                    page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(has_text="replacement.png").click()
                assert replaced_response.value.ok, replaced_response.value.text()
                response = page.request.get(f"{base}/api/runs/{run_id}/plan")
                assert response.ok, response.status
                edited_plan = response.json()
                edited_row = next(row for row in edited_plan["timeline"] if row["seq"] == failed_seq)
                assert edited_plan["revision"] == 2, edited_plan["revision"]
                assert edited_row["media"] == "replacement.png" and edited_row["source"] == "local", edited_row
                assert edited_row.get("needs_web") is False and not edited_row.get("asset_status"), edited_row
                assert model_calls == calls_before_edit, (calls_before_edit, model_calls)
                page.get_by_role("tab", name="网络素材").click()
                page.get_by_text(f"第 {failed_seq} 行已处理").wait_for(timeout=12000)
                page.get_by_text("当前时间线待替换 0 行").wait_for(timeout=12000)
                assert page.get_by_role("button", name=f"去替换第 {failed_seq} 行").count() == 0
                page.locator(".ant-tabs-tabpane-active .ant-typography-danger").filter(has_text="404 Client Error").wait_for(timeout=12000)
                page.screenshot(path=str(root / "ui-web-assets-resolved.png"), full_page=False)
                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.on("pageerror", lambda error: errors.append(str(error)))
                mobile.goto(base, wait_until="networkidle")
                mobile.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
                mobile.locator(".run-stage-picker .ant-select").click()
                mobile.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(has_text="网络素材").click()
                mobile.locator(".ant-select-dropdown:visible").wait_for(state="hidden", timeout=12000)
                mobile.get_by_text("已验证入库").first.wait_for(timeout=12000)
                mobile.get_by_text(f"第 {failed_seq} 行已处理").wait_for(timeout=12000)
                mobile.get_by_text("当前时间线待替换 0 行").wait_for(timeout=12000)
                assert mobile.evaluate("document.documentElement.scrollWidth") <= 390
                mobile.screenshot(path=str(root / "ui-web-assets-mobile.png"), full_page=False)
                assert not errors, errors
                report = {"run_id": run_id, "source": media_base, "statuses": [asset["status"] for asset in assets],
                          "verified_thumbnail_loaded": True, "verified_dimensions": [640, 480],
                          "manual_error": assets[1]["error"], "source_links": 2, "local_links": 1,
                          "replaced_row": failed_seq, "edit_revision": edited_plan["revision"],
                          "edit_model_calls": model_calls - calls_before_edit,
                          "mobile_width": mobile.evaluate("document.documentElement.scrollWidth"),
                          "browser_errors": errors}
                path = root / "web-assets-ui-verification.json"
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(path.resolve())
                browser.close()
    finally:
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            server_thread.join(10)
        media_server.shutdown()
        media_server.server_close()
        media_thread.join(5)
        runctl.set_runs_root_for_test(None)


if __name__ == "__main__":
    main()
