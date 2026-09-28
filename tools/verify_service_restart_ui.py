"""Verify browser refresh, service death, and Web resume during a long run."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from subprocess import DEVNULL, Popen
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4
import json
import socket
import sys
import time

from playwright.sync_api import sync_playwright
import uvicorn

from cut_agent import graph, runctl, vision
from cut_agent.config import ROOT
from cut_agent.llm import LLMError
from cut_agent.server import make_app


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def serve(runs_root: Path, port: int, delay: float) -> None:
    runctl.set_runs_root_for_test(runs_root.resolve())
    copy = (ROOT / "sample_copy.txt").read_text(encoding="utf-8")
    width = (len(copy) + 2) // 3
    texts = [copy[i:i + width] for i in range(0, len(copy), width)]

    def chat(system, user, **kwargs):
        if "分镜师" in system:
            time.sleep(delay)
            return {"segments": [{"text": text, "duration": 8, "role": role, "intensity": intensity}
                    for text, role, intensity in zip(texts, ("开头", "高潮", "收尾"), (2, 5, 2))]}
        if "时间线编排" in system:
            return {"timeline": [{"media": name, "shot_idx": 1, "segment_text": text}
                    for name, text in zip(("city_night.mp4", "mountain_day.mp4", "ocean_waves.mp4"), texts)]}
        return {"mood": "离线验证", "primary": {}, "alternatives": []}

    with ExitStack() as stack:
        stack.enter_context(patch.object(graph, "chat_json", chat))
        stack.enter_context(patch.object(graph, "VISION_ENABLED", True))
        stack.enter_context(patch.object(vision, "chat_vision", side_effect=LLMError("offline check")))
        stack.enter_context(patch.object(graph, "download_for_mood", return_value=[]))
        stack.enter_context(patch.object(graph, "controller", side_effect=RuntimeError("offline check")))
        uvicorn.run(make_app(), host="127.0.0.1", port=port, log_level="error", access_log=False)


def start_service(root: Path, port: int, delay: float) -> Popen:
    with (root / f"service-{int(delay)}.log").open("w", encoding="utf-8") as log:
        process = Popen([sys.executable, str(Path(__file__).resolve()), "--serve", str(root / "runs"),
                         str(port), str(delay)], cwd=ROOT, stdin=DEVNULL, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"服务提前退出: {process.returncode}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/stages", timeout=1) as response:
                    if response.status == 200:
                        return process
            except (OSError, URLError):
                time.sleep(.1)
        raise TimeoutError("服务未启动")
    except BaseException:
        stop_service(process)
        raise


def stop_service(process: Popen | None) -> None:
    if process is not None and process.poll() is None:
        process.kill()
        process.wait(timeout=10)


def main() -> None:
    root = Path("work/verification") / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:4])
    root.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    process = None
    try:
        process = start_service(root, port, 45)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=CHROME,
                                                 args=["--disable-gpu", "--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            copy = (ROOT / "sample_copy.txt").read_text(encoding="utf-8")
            response = page.request.post(f"{base}/api/runs",
                                         data={"media_dir": str(ROOT / "sample_media"), "copy": copy,
                                               "seed": 64, "preview": False, "finishing_llm": False})
            assert response.ok, response.text()
            run_id = response.json()["run_id"]
            page.goto(f"{base}?run={run_id}&stage=plan_segments", wait_until="domcontentloaded")
            page.get_by_text("当前流程节点：第 2/8 阶段 · 文案分段").wait_for(timeout=30000)
            assert page.request.get(f"{base}/api/runs/{run_id}").json()["status"] == "running"
            scan_file = root / "runs" / run_id / "stages" / "scan_media.json"
            assert scan_file.is_file()
            scan_bytes = scan_file.read_bytes()
            page.wait_for_timeout(10000)
            assert page.request.get(f"{base}/api/runs/{run_id}").json()["status"] == "running"
            page.reload(wait_until="domcontentloaded")
            page.get_by_text("当前流程节点：第 2/8 阶段 · 文案分段").wait_for(timeout=15000)
            assert page.get_by_role("tab", name="文案分段").get_attribute("aria-selected") == "true"
            page.screenshot(path=str(root / "long-run-after-refresh.png"), full_page=False)

            stopped_pid = process.pid
            stop_service(process)
            process = None
            page.get_by_text("连接中断，正在自动重试").wait_for(timeout=15000)
            assert page.get_by_text("当前流程节点：第 2/8 阶段 · 文案分段").count() == 1
            page.screenshot(path=str(root / "service-down.png"), full_page=False)
            process = start_service(root, port, 0)
            page.wait_for_timeout(3000)
            page.screenshot(path=str(root / "service-restarted-diagnostic.png"), full_page=False)
            page.get_by_role("button", name="恢复").wait_for(timeout=20000)
            assert page.get_by_role("tab", name="文案分段").get_attribute("aria-selected") == "true"
            interrupted = page.request.get(f"{base}/api/runs/{run_id}").json()
            assert interrupted["status"] == "interrupted"
            assert interrupted["resumable_from"] == "plan_segments"
            page.screenshot(path=str(root / "restarted-interrupted.png"), full_page=False)
            page.get_by_role("button", name="恢复").click()
            page.get_by_text("流程已完成：8/8 阶段").wait_for(timeout=45000)
            assert scan_file.read_bytes() == scan_bytes
            assert page.request.get(f"{base}/api/runs/{run_id}").json()["status"] == "done"
            page.screenshot(path=str(root / "resumed-done.png"), full_page=False)
            mobile = browser.new_page(viewport={"width": 390, "height": 844})
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            mobile.goto(f"{base}?run={run_id}&stage=write_doc", wait_until="networkidle")
            mobile.get_by_text("流程已完成：8/8 阶段").wait_for(timeout=15000)
            assert mobile.evaluate("document.documentElement.scrollWidth") <= 390
            mobile.screenshot(path=str(root / "resumed-mobile-document.png"), full_page=False)
            assert not errors, errors
            report = {"run": run_id, "initial_service_pid": stopped_pid,
                      "restarted_service_pid": process.pid, "running_observed_seconds": 10,
                      "browser_refresh_kept_stage": "plan_segments",
                      "service_down_kept_last_view": True,
                      "recovered_status": interrupted["status"],
                      "resumable_from": interrupted["resumable_from"],
                      "scan_stage_reused": True, "final_status": "done",
                      "mobile_width": mobile.evaluate("document.documentElement.scrollWidth"),
                      "browser_errors": errors}
            path = root / "service-restart-ui-verification.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(path.resolve())
            browser.close()
    finally:
        stop_service(process)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--serve":
        serve(Path(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]))
    else:
        main()
