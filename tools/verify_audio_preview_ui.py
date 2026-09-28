"""Play a real mixed-audio preview in Chrome and measure its decoded test tone."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from uuid import uuid4
import json
import socket
import subprocess
import time

from playwright.sync_api import sync_playwright
import uvicorn

from cut_agent import graph, render, runs, runctl, vision
from cut_agent.config import ROOT
from cut_agent.llm import LLMError
from cut_agent.server import make_app


CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def main() -> None:
    root = Path("work/verification") / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:4])
    root.mkdir(parents=True)
    runctl.set_runs_root_for_test((root / "runs").resolve())
    tone = (root / "tone-440hz-24s.wav").resolve()
    subprocess.run([render._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=24", str(tone)], check=True)
    assert tone.stat().st_size > 10_000
    copy = (ROOT / "sample_copy.txt").read_text(encoding="utf-8")
    width = (len(copy) + 2) // 3
    texts = [copy[i:i + width] for i in range(0, len(copy), width)]

    def chat(system, user, **kwargs):
        if "分镜师" in system:
            return {"segments": [{"text": text, "duration": 8, "role": role, "intensity": strength}
                    for text, role, strength in zip(texts, ["开头", "高潮", "收尾"], [2, 5, 2])]}
        if "时间线编排" in system:
            return {"timeline": [{"media": name, "shot_idx": 1, "segment_text": text}
                    for name, text in zip(["city_night.mp4", "mountain_day.mp4", "ocean_waves.mp4"], texts)]}
        return {"mood": "测试音", "primary": {"title": "440 Hz 测试音"}, "alternatives": []}

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    server = None
    thread = None
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(graph, "chat_json", chat))
            stack.enter_context(patch.object(graph, "VISION_ENABLED", True))
            stack.enter_context(patch.object(vision, "chat_vision", side_effect=LLMError("offline verification")))
            stack.enter_context(patch.object(graph, "download_for_mood", return_value=[
                {"title": "440 Hz test tone", "url": "https://example.invalid/test-tone",
                 "local": str(tone), "size_kb": tone.stat().st_size // 1024}]))
            stack.enter_context(patch.object(graph, "controller", side_effect=RuntimeError("offline verification")))
            handle, _ = runs.start_run(str(ROOT / "sample_media"), copy, seed=7,
                                       options={"preview": True, "finishing_llm": False}, sync=True)
            run_id = handle.run_id
            assert runs.status_of(run_id)["status"] == "done"
            run_root = runctl.run_dir(run_id)
            plan = runctl.read_json(run_root / "plan.json")
            preview = plan["preview"]
            assert preview["status"] == "ready" and preview["has_music"]
            markers = preview["markers"]
            assert len(markers) == 3 and abs(preview["duration"] - 24) < .1
            audio_streams = json.loads(subprocess.check_output([
                render._ffprobe(), "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=codec_name,sample_rate", "-of", "json",
                str(run_root / "preview.mp4")], text=True))["streams"]
            assert audio_streams and audio_streams[0]["codec_name"] == "aac"

            server = uvicorn.Server(uvicorn.Config(make_app(), host="127.0.0.1", port=port, log_level="error"))
            thread = Thread(target=server.run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                time.sleep(.05)
            assert server.started

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=CHROME,
                    args=["--disable-gpu", "--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base, wait_until="networkidle")
                page.get_by_role("row").filter(has_text=run_id).locator("button").first.click()
                page.get_by_role("tab", name="文档产出").click()
                page.locator("#preview-player").get_by_text("已混入配乐").wait_for(timeout=12000)
                video = page.locator("#preview-player video")
                page.wait_for_function("() => { const v = document.querySelector('#preview-player video'); return v && v.videoWidth > 0 && Number.isFinite(v.duration) && v.duration > 0; }", timeout=15000)
                video.evaluate("""async video => {
                  const context = new AudioContext();
                  const source = context.createMediaElementSource(video);
                  const analyser = context.createAnalyser();
                  analyser.fftSize = 4096;
                  source.connect(analyser);
                  analyser.connect(context.destination);
                  await context.resume();
                  const samples = [];
                  window.audioProbe = { context, analyser, samples };
                  video.muted = false;
                  video.volume = 1;
                  video.currentTime = 0;
                  await video.play();
                  window.audioProbe.timer = setInterval(() => {
                    const waveform = new Float32Array(analyser.fftSize);
                    analyser.getFloatTimeDomainData(waveform);
                    const rms = Math.sqrt(waveform.reduce((sum, value) => sum + value * value, 0) / waveform.length);
                    const spectrum = new Float32Array(analyser.frequencyBinCount);
                    analyser.getFloatFrequencyData(spectrum);
                    let peak = 8;
                    for (let i = 9; i < Math.min(200, spectrum.length); i++)
                      if (spectrum[i] > spectrum[peak]) peak = i;
                    samples.push({ time: video.currentTime, rms, peakHz: peak * context.sampleRate / analyser.fftSize });
                  }, 100);
                }""")
                page.wait_for_function("markers => markers.every(marker => window.audioProbe?.samples.some(sample => sample.time > marker.start + .35 && sample.time < marker.end && sample.rms > .002 && Math.abs(sample.peakHz - 440) < 45))", arg=markers, timeout=20000)
                page.locator("#preview-player").screenshot(path=str(root / "ui-audio-preview-playing.png"))
                page.wait_for_function("() => document.querySelector('#preview-player video')?.ended", timeout=15000)
                samples = page.evaluate("""() => {
                  clearInterval(window.audioProbe.timer);
                  return window.audioProbe.samples;
                }""")
                assert not errors, errors
                browser.close()

            report = {"run_id": run_id, "preview_seconds": preview["duration"],
                      "segments": len(markers), "source_tone_hz": 440,
                      "audio_stream": audio_streams[0],
                      "browser_audio_segments_detected": [
                          marker["seq"] for marker in markers if any(
                              marker["start"] + .35 < sample["time"] < marker["end"]
                              and sample["rms"] > .002 and abs(sample["peakHz"] - 440) < 45
                              for sample in samples)],
                      "max_browser_rms": round(max(sample["rms"] for sample in samples), 5),
                      "browser_peak_hz": round(max(samples, key=lambda sample: sample["rms"])["peakHz"], 2),
                      "browser_errors": errors, "model": "offline fixed replies",
                      "music": "synthetic test tone; not a subjective listening test"}
            path = root / "audio-preview-ui-verification.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(path.resolve())
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(10)
        runctl.set_runs_root_for_test(None)


if __name__ == "__main__":
    main()
