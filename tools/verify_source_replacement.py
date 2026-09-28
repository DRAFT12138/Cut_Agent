"""Create a real run and switch a downloaded blue asset to red local image/video.

Uses actual edit/preview HTTP routes and forbids model calls after fixture setup.
uv run --no-sync python tools/verify_source_replacement.py
"""
import json
import shutil
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from cut_agent import graph, media, render, runctl, runs, vision
from cut_agent.config import ROOT
from cut_agent.editing import load_plan, rebuild
from cut_agent.llm import LLMError
from cut_agent.server import make_app


def check(plan, root, color, evidence, name):
    card = plan["storyboard"]["cards"][0]
    assert not card["frame_missing"], card
    with Image.open(root / card["frame"]) as image:
        pixel = image.convert("RGB").getpixel((320, 240))
        assert pixel[color] > 220 and max(pixel[n] for n in range(3) if n != color) < 30
        image.save(evidence / f"{name}.png")
    preview = plan["preview"]
    assert preview["status"] == "ready", preview
    raw = subprocess.run([render._ffmpeg(), "-v", "error", "-i", preview["path"],
        "-vf", "format=rgb24,crop=1:1:640:360", "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    pixels = [list(raw[n:n + 3]) for n in range(0, len(raw), 3)]
    assert len(pixels) == card["timeline_out_frame"] == preview["frame_count"]
    assert all(p[color] > 220 and max(p[n] for n in range(3) if n != color) < 30 for p in pixels)
    shutil.copy2(preview["path"], evidence / f"{name}.mp4")
    assert plan["finishing"]["steps"][0]["anchor"] == card
    return {"name": name, "revision": plan["revision"], "row": plan["timeline"][0],
            "card": card, "preview_frames": len(pixels), "center_pixels": pixels}


def main():
    evidence = ROOT / "work" / "verification" / runctl.new_run_id()
    folder = evidence / "media"
    folder.mkdir(parents=True)
    Image.new("RGB", (640, 480), "red").save(folder / "red.png")
    Image.new("RGB", (640, 480), "blue").save(evidence / "web-blue.png")
    for dest, color, fps in [(folder / "red.mp4", "red", 25), (evidence / "web-blue.mp4", "blue", 10)]:
        subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color={color}:size=640x480:rate={fps}:duration=0.8",
                        "-c:v", "libx264", str(dest)], check=True)
    remote = media._probe_video(evidence / "web-blue.mp4", evidence)
    def chat(system, user, **kwargs):
        if "分镜师" in system:
            return {"segments": [{"text": "Keep narration", "duration": .4, "role": "高潮", "intensity": 4}]}
        if "时间线编排" in system:
            return {"timeline": [{"source": "local", "media": "red.png", "use_duration": .4}]}
        return {"mood": "verification", "primary": {}, "alternatives": []}
    with patch.object(graph, "chat_json", chat), patch.object(graph, "VISION_ENABLED", True), \
         patch.object(vision, "chat_vision", side_effect=LLMError("offline verification")), \
         patch.object(graph, "download_for_mood", return_value=[]):
        handle, _ = runs.start_run(str(folder), "Keep narration", seed=7,
                                  options={"finishing_llm": False}, sync=True)
    assert runs.status_of(handle.run_id)["status"] == "done"
    root = runctl.run_dir(handle.run_id)
    plan = load_plan(handle.run_id)
    plan["timeline"] = [{"source": "web", "kind": "video", "media": "downloaded-blue", "use_duration": .4,
        "asset_status": "verified", "local_path": str(remote.path), "ref": str(remote.path),
        "thumbnail": str(evidence / "web-blue.png"), "source_url": "https://example.test/blue",
        "source_fps": remote.fps, "source_timing": remote.source_timing, "segment_text": "Keep narration",
        "role": "高潮", "intensity": 4, "note": "Keep editorial note"}]
    runctl.write_json_atomic(root / "plan.json", plan)
    def forbidden(*args, **kwargs):
        raise AssertionError("source replacement must not call a model")
    checked = []
    with patch.object(graph, "chat_json", forbidden), TestClient(make_app()) as client:
        original = rebuild(handle.run_id, preview=True)
        checked.append(check(original, root, 2, evidence, "before-web"))
        for name in ("red.png", "red.mp4"):
            response = client.post(f"/api/runs/{handle.run_id}/edit",
                json={"op": "swap", "row": 1, "media": name, "expected_revision": checked[-1]["revision"]})
            assert response.status_code == 200, response.text
            assert response.json()["preview"]["status"] == "stale"
            response = client.post(f"/api/runs/{handle.run_id}/preview")
            assert response.status_code == 200, response.text
            changed = response.json()
            row = changed["timeline"][0]
            assert row["source"] == "local" and row["media"] == name
            assert not {"asset_status", "local_path", "thumbnail", "ref", "source_url",
                        "source_fps", "source_timing"}.intersection(row)
            assert row["segment_text"] == "Keep narration" and "Keep editorial note" in row["note"]
            assert row["role"] == "高潮" and row["intensity"] == 4
            for stage in ("build_timeline", "explore_web", "write_doc"):
                assert runs.stage_artifact(handle.run_id, stage)["timeline"] == changed["timeline"]
            assert changed["storyboard"]["cards"][0]["source_fps"] == 25
            checked.append(check(changed, root, 0, evidence, "after-" + name))
    report = {"result": "passed", "run_id": handle.run_id, "checks": checked, "llm_calls_during_edits": 0,
              "scope": "真实素材、HTTP 换镜/预览刷新、全部预览帧色值与同源阶段；浏览器操作未验收"}
    runctl.write_json_atomic(evidence / "source-replacement-verification.json", report)
    print(json.dumps({"result": "passed", "run_id": handle.run_id, "edits": 2,
                      "report": str(evidence / "source-replacement-verification.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
