import subprocess

from PIL import Image
import pytest

from cut_agent import graph, media, render, runctl, runs
from cut_agent.editing import load_plan, rebuild


WEB_FIELDS = {"asset_status", "local_path", "ref", "thumbnail", "source_url", "source_fps", "source_timing"}


def make_sources(root, kind):
    folder = root / "media"
    folder.mkdir(parents=True)
    red = folder / "red.png"
    blue = root / "web-blue.png"
    Image.new("RGB", (640, 480), "red").save(red)
    Image.new("RGB", (640, 480), "blue").save(blue)
    web = root / "web-blue.mp4"
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=blue:size=640x480:rate=10:duration=0.8", "-c:v", "libx264", str(web)], check=True)
    remote = media._probe_video(web, root)
    if kind == "image":
        item = media._probe_image(red, folder)
    else:
        source = folder / "red.mp4"
        subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=red:size=640x480:rate=25:duration=0.8", "-c:v", "libx264", str(source)], check=True)
        item = media._probe_video(source, folder)
    local = {**vars(item), "path": str(item.path)}
    if kind == "video":
        local["shots"] = [{"idx": 1, "start": 0, "end": item.duration, "motion": 1,
                           "description": "red local shot", "frames": [str(red)], "frame_times": [0]}]
    row = {"source": "web", "kind": "video", "media": "downloaded-blue", "use_duration": .4,
           "asset_status": "verified", "local_path": str(web), "ref": str(web), "thumbnail": str(blue),
           "source_url": "https://example.test/blue", "source_fps": remote.fps,
           "source_timing": remote.source_timing, "segment_text": "Keep this narration", "role": "高潮",
           "intensity": 4, "note": "Keep this editorial note"}
    return folder, local, row


def assert_pixels(plan, root, channel):
    card = plan["storyboard"]["cards"][0]
    assert not card["frame_missing"]
    with Image.open(root / card["frame"]) as image:
        pixel = image.convert("RGB").getpixel((320, 240))
        assert pixel[channel] > 220 and max(pixel[n] for n in range(3) if n != channel) < 30
    preview = plan["preview"]
    assert preview["status"] == "ready", preview
    pixels = subprocess.run([render._ffmpeg(), "-v", "error", "-i", preview["path"],
        "-vf", "format=rgb24,crop=1:1:640:360", "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    assert len(pixels) == card["timeline_out_frame"] * 3
    for n in range(0, len(pixels), 3):
        pixel = pixels[n:n + 3]
        assert pixel[channel] > 220 and max(pixel[c] for c in range(3) if c != channel) < 30


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("operation", ["swap", "direct_json"])
def test_web_to_local_edit_rebinds_all_exports(tmp_path, tmp_runs, fake_llm, monkeypatch, kind, operation):
    handle, _ = runs.start_run("unused", "source replacement", sync=True)
    root = runctl.run_dir(handle.run_id)
    folder, local, row = make_sources(tmp_path / "sources", kind)
    plan = load_plan(handle.run_id)
    plan.update(media_folder=str(folder), media=[local], timeline=[row], segments=[], web_assets=[])
    plan["_ctx"] = graph.RunCtx(handle.run_id, run_dir=root, options={"preview": True, "finishing_llm": False})
    monkeypatch.setattr(graph, "chat_json", lambda *a, **kw: pytest.fail("source editing must not call LLM"))
    plan.update(graph.write_doc(plan))
    assert_pixels(plan, root, 2)  # A still-verified web row continues using its blue downloaded video.
    if operation == "direct_json":
        saved = load_plan(handle.run_id)
        saved["timeline"][0].update(source="local", media=local["name"])
        runctl.write_json_atomic(root / "plan.json", saved)
        changed = rebuild(handle.run_id, preview=True)
    else:
        changed = rebuild(handle.run_id, {"op": "swap", "row": 1, "media": local["name"], "shot": 1}, preview=True)
    assert_pixels(changed, root, 0)
    selected = changed["timeline"][0]
    assert not WEB_FIELDS.intersection(selected)
    assert (selected["source"], selected["kind"], selected["media"]) == ("local", kind, local["name"])
    for key in ("segment_text", "role", "intensity", "note"):
        assert selected[key] == row[key]
    card = changed["storyboard"]["cards"][0]
    assert card["source_fps"] == 25
    assert card["source_fps_assumed"] == (kind == "image")
    assert changed["finishing"]["steps"][0]["anchor"] == card
    assert runs.stage_artifact(handle.run_id, "explore_web")["timeline"] == changed["timeline"]
    assert "red." in (root / "cut_lines.txt").read_text(encoding="utf-8")


def test_legacy_local_row_ignores_stale_web_paths_without_rebinding(tmp_path):
    folder, local, row = make_sources(tmp_path / "sources", "image")
    row.update(source="local", media=local["name"], kind="image")
    root = tmp_path / "legacy-export"
    state = {"media_folder": str(folder), "media": [local], "timeline": [row],
             "_ctx": graph.RunCtx("legacy", run_dir=root, options={"preview": True})}
    state.update(graph.write_doc(state))
    assert_pixels(state, root, 0)
    card = state["storyboard"]["cards"][0]
    assert card["source_fps"] == 25 and card["source_fps_assumed"]
