import json
from pathlib import Path
import subprocess

from PIL import Image
import pytest
from fastapi.testclient import TestClient

from cut_agent import graph, render, runctl, runs, storyboard
from cut_agent.editing import rebuild
from cut_agent.server import make_app


def test_preview_opt_in_and_failure_isolation(tmp_runs, fake_llm, monkeypatch):
    monkeypatch.setattr(render, "render_video", lambda *a, **kw: pytest.fail("must be opt-in"))
    h, _ = runs.start_run("unused", "test", sync=True)
    assert runs.status_of(h.run_id)["status"] == "done"
    assert not (runctl.run_dir(h.run_id) / "preview.mp4").exists()
    def fail(*a, **kw):
        raise render.RenderError("encoder unavailable")
    monkeypatch.setattr(render, "render_video", fail)
    h, _ = runs.start_run("unused", "test", options={"preview": True}, sync=True)
    assert runs.status_of(h.run_id)["status"] == "done"
    plan = runctl.read_json(runctl.run_dir(h.run_id) / "plan.json")
    assert plan["preview"]["status"] == "failed"
    assert (runctl.run_dir(h.run_id) / "plan.md").is_file()


def test_real_preview_labels_missing_rows_and_resume(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    Image.new("RGB", (640, 480), "red").save(source)
    timeline = [{"media": source.name, "kind": "image", "use_duration": .4},
                {"source": "web", "kind": "video", "use_duration": .4}]
    output = tmp_path / "preview.mp4"
    first = render.render_video(timeline, tmp_path, {}, None, output)
    assert first["n_segments"] == 2 and first["skipped"]
    assert first["markers"][1]["start"] == .4
    assert .75 <= render._duration_of(output) <= .85
    assert first["fps"] == 25 and first["bgm_volume"] == .4
    frame = tmp_path / "check.png"
    subprocess.run([render._ffmpeg(), "-v", "error", "-y", "-i", str(output), "-frames:v", "1", str(frame)], check=True)
    with Image.open(frame) as image:
        # White label pixels over the red/black source prove that drawtext was encoded.
        region = image.convert("RGB").crop((16, 16, 650, 50))
        assert any(min(pixel) > 190 for pixel in region.get_flattened_data())
    monkeypatch.setattr(render, "normalize_segment", lambda *a, **kw: pytest.fail("completed segment must be reused"))
    music = tmp_path / "tone.wav"
    subprocess.run([render._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.8", str(music)], check=True)
    second = render.render_video(timeline, tmp_path, {}, music, output)
    assert second["n_segments"] == 2 and second["has_music"]


def test_preview_mixes_source_silence_narration_and_looped_bgm(tmp_path):
    voiced = tmp_path / "voiced.mp4"
    silent = tmp_path / "silent.mp4"
    narration = tmp_path / "narration.wav"
    music = tmp_path / "short-bgm.wav"
    subprocess.run([render._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=red:size=160x90:rate=25:duration=.4", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=.4", "-c:v", "libx264", "-c:a", "aac",
                    "-shortest", str(voiced)], check=True)
    subprocess.run([render._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=blue:size=160x90:rate=25:duration=.4", "-c:v", "libx264", str(silent)], check=True)
    for path, spec in ((narration, "sine=frequency=900:duration=.8"),
                       (music, "sine=frequency=120:duration=.2")):
        subprocess.run([render._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", spec,
                        str(path)], check=True)
    result = render.render_video(
        [{"media": voiced.name, "kind": "video", "use_duration": .4},
         {"media": silent.name, "kind": "video", "use_duration": .4}],
        tmp_path, {}, music, tmp_path / "preview.mp4", narration_path=narration,
        narration_segments=[{"source_start": 0, "source_end": .4, "timeline_start": 0},
                            {"source_start": .4, "source_end": .8, "timeline_start": .4}],
        source_volume=.7, narration_volume=.9, ducking_db=-10)
    assert result["has_music"] and result["has_narration"]
    assert result["audio_warnings"] == ["#2 silent.mp4 无源音轨，已补等时长静音"]
    assert [track["type"] for track in result["audio_tracks"]] == ["source", "narration", "bgm"]
    assert result["mix_fingerprint"]["ducking_db"] == -10
    probe = subprocess.run([render._ffprobe(), "-v", "error", "-show_entries",
                            "stream=codec_type,duration", "-of", "json", result["path"]],
                           capture_output=True, text=True, check=True)
    streams = json.loads(probe.stdout)["streams"]
    video_duration = float(next(row["duration"] for row in streams if row["codec_type"] == "video"))
    audio_duration = float(next(row["duration"] for row in streams if row["codec_type"] == "audio"))
    assert audio_duration == pytest.approx(video_duration, abs=.04)


def test_preview_route_range_and_edit_invalidation(tmp_runs, fake_llm):
    h, _ = runs.start_run("unused", "test", sync=True)
    root = runctl.run_dir(h.run_id)
    payload = b"preview-test-payload"
    (root / "preview.mp4").write_bytes(payload)
    plan = runctl.read_json(root / "plan.json")
    plan["preview"] = {"status": "ready", "path": str(root / "preview.mp4")}
    runctl.write_json_atomic(root / "plan.json", plan)
    with TestClient(make_app()) as client:
        response = client.get(f"/api/runs/{h.run_id}/preview", headers={"Range": "bytes=0-6"})
        assert response.status_code == 206 and response.content == payload[:7]
        rebuild(h.run_id, {"op": "duration", "row": 1, "seconds": 2})
        assert client.get(f"/api/runs/{h.run_id}/preview").status_code == 404


def test_fractional_row_durations_use_cumulative_frame_anchors(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (64, 64), "blue").save(source)
    timeline = [{"media": source.name, "kind": "image", "use_duration": .06} for _ in range(3)]
    result = render.render_video(timeline, tmp_path, {}, None, tmp_path / "preview.mp4")
    assert [m["start"] for m in result["markers"]] == [0, .08, .12]
    assert abs(result["duration"] - .2) < .01


def test_short_video_fails_preview_without_compressing_plan(tmp_path):
    source = tmp_path / "short-picture.mp4"
    # A real container with 1s of video and 2s of audio reports 2s overall duration.
    subprocess.run([render._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=red:size=160x90:rate=10:duration=1", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:v", "libx264", "-c:a", "aac",
                    str(source)], check=True)
    assert render._duration_of(source) >= 2
    output = tmp_path / "run"
    output.mkdir()
    previous = output / "preview.mp4"
    previous.write_bytes(b"previous published preview")
    state = {"media_folder": str(tmp_path), "copy": "短片检验", "media": [],
             "timeline": [{"media": source.name, "kind": "video", "use_duration": 1.2},
                          {"source": "web", "use_duration": .4}],
             "_ctx": graph.RunCtx("short-test", run_dir=output, options={"preview": True})}
    result = graph.write_doc(state)
    assert result["preview"]["status"] == "failed"
    assert "需要 30 帧，实际解码 25 帧" in result["preview"]["error"]
    plan = runctl.read_json(output / "plan.json")
    assert plan["timeline"] == state["timeline"]
    assert plan["storyboard"]["cards"][-1]["timeline_out_frame"] == 40
    assert (output / "plan.md").is_file()
    assert previous.read_bytes() == b"previous published preview"
    assert not (output / "preview_work" / "seg_001.json").exists()


def test_truncated_completed_segment_is_rebuilt(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    Image.new("RGB", (64, 64), "red").save(source)
    timeline = [{"media": source.name, "kind": "image", "use_duration": .2} for _ in range(2)]
    output = tmp_path / "preview.mp4"
    render.render_video(timeline, tmp_path, {}, None, output)
    segment = tmp_path / "preview_work" / "seg_001.mp4"
    second = (segment.parent / "seg_002.mp4").read_bytes()
    render.normalize_segment(source, "image", 0, .08, segment, fps=25)
    calls = []
    encode = render.normalize_segment
    def record(*args, **kwargs):
        calls.append(args[4].name)
        return encode(*args, **kwargs)
    monkeypatch.setattr(render, "normalize_segment", record)
    result = render.render_video(timeline, tmp_path, {}, None, output)
    assert calls == ["seg_001.partial.mp4"]
    assert (segment.parent / "seg_002.mp4").read_bytes() == second
    assert result["frame_count"] == 10 and result["duration"] == .4


def test_incomplete_concat_does_not_replace_previous_preview(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    Image.new("RGB", (64, 64), "blue").save(source)
    timeline = [{"media": source.name, "kind": "image", "use_duration": .2} for _ in range(2)]
    output = tmp_path / "preview.mp4"
    output.write_bytes(b"previous preview")
    def incomplete(paths, dest):
        dest.write_bytes(paths[0].read_bytes())
        return True
    monkeypatch.setattr(render, "_concat_copy", incomplete)
    with pytest.raises(render.RenderError, match="最终预览.*需要 10 帧，实际解码 5 帧"):
        render.render_video(timeline, tmp_path, {}, None, output)
    assert output.read_bytes() == b"previous preview"


@pytest.mark.parametrize("fps", [25, 30000 / 1001])
def test_encoded_frame_timestamps_match_storyboard(tmp_path, fps):
    timeline = []
    for i, color in enumerate(["#ff0000", "#00ff00", "#0000ff"]):
        source = tmp_path / f"source_{i}.png"
        Image.new("RGB", (64, 64), color).save(source)
        timeline.append({"media": source.name, "kind": "image", "use_duration": .06})
    output = tmp_path / "preview.mp4"
    board = storyboard.build_storyboard({"media_folder": str(tmp_path), "timeline": timeline},
                                       tmp_path / "board", timeline_fps=fps)
    result = render.render_video(timeline, tmp_path, {}, None, output, fps=fps)
    probe = subprocess.run([render._ffprobe(), "-v", "error", "-select_streams", "v:0",
                            "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
                            "-of", "json", str(output)], capture_output=True, text=True, check=True)
    timestamps = [float(f["best_effort_timestamp_time"]) for f in json.loads(probe.stdout)["frames"]]
    assert len(timestamps) == board["cards"][-1]["timeline_out_frame"] == result["frame_count"]
    assert timestamps == pytest.approx([i / fps for i in range(len(timestamps))], abs=.0001)
    decoded = subprocess.run([render._ffmpeg(), "-v", "error", "-i", str(output),
                              "-vf", "format=rgb24,crop=1:1:640:360", "-fps_mode", "passthrough",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    assert len(decoded.stdout) == len(timestamps) * 3
    for i, (card, marker) in enumerate(zip(board["cards"], result["markers"])):
        assert marker["start"] == card["timeline_in_frame"] / fps
        assert marker["end"] == card["timeline_out_frame"] / fps
        for n in range(card["timeline_in_frame"], card["timeline_out_frame"]):
            pixel = decoded.stdout[n * 3:n * 3 + 3]
            assert pixel[i] > 220 and max(pixel[c] for c in range(3) if c != i) < 30


def test_relative_export_path_with_filter_special_characters(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = Path("编辑员's [cut], final; version")
    root.mkdir()
    source = root / "source's.png"
    Image.new("RGB", (64, 64), "red").save(source)
    monkeypatch.setattr(render, "_concat_reencode", lambda *args: pytest.fail("copy concat should work"))
    result = render.render_video(
        [{"media": source.name, "kind": "image", "use_duration": .2} for _ in range(2)],
        root, {}, None, root / "preview.mp4")
    assert result["frame_count"] == 10 and result["duration"] == .4


@pytest.mark.parametrize("extension,animated", [("gif", True), ("gif", False), ("webp", True), ("png", True)])
def test_image_formats_hold_first_frame_for_full_preview(tmp_path, monkeypatch, extension, animated):
    source = tmp_path / f"source.{extension}"
    first = Image.new("RGB", (320, 240), "red")
    if animated:
        first.save(source, save_all=True, append_images=[Image.new("RGB", first.size, "blue")],
                   duration=200, loop=0, lossless=True)
        with Image.open(source) as image:
            assert image.is_animated and image.n_frames == 2
            image.seek(1)
            assert image.convert("RGB").getpixel((160, 120)) == (0, 0, 255)
    else:
        first.save(source)
    original = source.read_bytes()
    timeline = [{"media": source.name, "source": "local", "kind": "image", "use_duration": 1.2}]
    board = storyboard.build_storyboard({"media_folder": str(tmp_path), "timeline": timeline,
        "media": [{"name": source.name, "kind": "image"}]}, tmp_path / "board")
    assert not board["cards"][0]["frame_missing"]
    with Image.open(tmp_path / "board" / board["cards"][0]["frame"]) as image:
        assert image.convert("RGB").getpixel((160, 120)) == (255, 0, 0)
    output = tmp_path / "preview.mp4"
    result = render.render_video(timeline, tmp_path, {}, None, output)
    assert result["frame_count"] == 30 and result["duration"] == 1.2
    decoded = subprocess.run([render._ffmpeg(), "-v", "error", "-i", str(output),
                              "-vf", "format=rgb24,crop=1:1:640:360", "-fps_mode", "passthrough",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    assert len(decoded.stdout) == 30 * 3
    for n in range(30):
        pixel = decoded.stdout[n * 3:n * 3 + 3]
        assert pixel[0] > 220 and max(pixel[1:]) < 30
    assert source.read_bytes() == original
    monkeypatch.setattr(render, "normalize_segment", lambda *a, **kw: pytest.fail("completed still segment must be reused"))
    assert render.render_video(timeline, tmp_path, {}, None, output)["frame_count"] == 30
