import json
import os
from pathlib import Path
import subprocess

from PIL import Image
import pytest

from cut_agent import config, graph, media, runctl, vision
from cut_agent.finishing import cover_candidates
from cut_agent.llm import LLMError
from cut_agent.media import _ffmpeg


def sample_analysis(src, duration, work_dir):
    frame = work_dir / "frame.png"
    frame.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 24), "red").save(frame)
    shot = vision.Shot(1, 0, duration, 0, 2, frames=[frame], frame_times=[1], frame_motions=[4])
    return vision.VideoVision(src.name, src, duration, shots=[shot], frames=[frame], frame_times=[1])


def test_offline_variants_reuse_geometry_but_retry_semantics(tmp_path, monkeypatch):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"source")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(graph, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(graph, "VISION_ENABLED", True)
    calls, model_calls = [], []
    online = False

    def analyse(*args, **kwargs):
        calls.append(1)
        return sample_analysis(*args, **kwargs)

    def describe(*args, **kwargs):
        model_calls.append(1)
        if not online:
            raise LLMError("offline")
        return json.dumps({"shots": [{"shot_idx": 1, "description": "red frame", "roles": ["开头"]}]})

    monkeypatch.setattr(vision, "analyse_video", analyse)
    monkeypatch.setattr(vision, "chat_vision", describe)
    def run(name):
        return graph.understand_media({"media_folder": str(tmp_path),
            "media": [{"name": src.name, "kind": "video", "duration": 4}],
            "_ctx": graph.RunCtx(name, run_dir=tmp_path / name)})["media"][0]

    first = run("A")
    assert not first["geometry_reused"] and not first["description"]
    Path(first["shots"][0]["frames"][0]).unlink()  # cache owns an independent copy
    previous_calls = len(model_calls)
    second = run("B")
    assert second["geometry_reused"] and len(calls) == 1
    assert len(model_calls) > previous_calls and not second["description"]
    assert Path(second["shots"][0]["frames"][0]).is_relative_to(tmp_path / "B")
    assert second["shots"][0]["frame_motions"] == [4]
    online = True
    third = run("C")
    assert third["geometry_reused"] and third["shots"][0]["description"] == "red frame"
    assert len(calls) == 1
    monkeypatch.setattr(config, "LLM_MODEL", "different model")
    assert run("D")["geometry_reused"] and len(calls) == 1
    monkeypatch.setattr(vision, "FRAME_SPACING", .7)
    assert not run("E")["geometry_reused"] and len(calls) == 2
    src.write_bytes(b"source changed")
    assert not run("F")["geometry_reused"] and len(calls) == 3


def test_geometry_is_committed_before_pause_or_model_request(tmp_path, monkeypatch):
    src = tmp_path / "clip.mp4"
    src.touch()
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    control = runctl.RunControl("paused")
    calls = []
    def analyse(*args, **kwargs):
        calls.append(1)
        control.request_pause()
        return sample_analysis(*args, **kwargs)
    monkeypatch.setattr(vision, "analyse_video", analyse)
    monkeypatch.setattr(vision, "chat_vision", lambda *a, **kw: pytest.fail("pause precedes model request"))
    with runctl.control_scope(control), pytest.raises(runctl.RunHalted):
        vision.analyse_with_llm(src, 4, tmp_path / "A")
    monkeypatch.setattr(vision, "chat_vision", lambda *a, **kw: '{"shots":[{"shot_idx":1,"description":"red"}]}')
    result = vision.analyse_with_llm(src, 4, tmp_path / "B")
    assert result.geometry_reused and len(calls) == 1
    assert result.shots[0].description == "red"


def test_corrupt_cached_image_is_recomputed(tmp_path, monkeypatch):
    src = tmp_path / "clip.mp4"
    src.touch()
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(vision, "analyse_video", sample_analysis)
    monkeypatch.setattr(vision, "chat_vision", lambda *a, **kw: '{}')
    vision.analyse_with_llm(src, 4, tmp_path / "A")
    cached = list((tmp_path / "work/media_cache/geometry/frames").rglob("*.png"))
    assert len(cached) == 1
    cached[0].write_bytes(b"not an image")
    result = vision.analyse_with_llm(src, 4, tmp_path / "B")
    assert not result.geometry_reused
    with Image.open(result.frames[0]) as image:
        image.verify()


def test_representative_timestamps_and_motion_match_the_sampled_grid(tmp_path, monkeypatch):
    candidates = [(i / 4, Image.new("RGB", (8, 8), (value,) * 3))
                  for i, value in enumerate([0, 10, 20, 40, 60])]
    monkeypatch.setattr(vision, "_extract_rgb_thumbs", lambda *a: candidates)
    monkeypatch.setattr(vision, "_allocate", lambda *a: [{"t": .31}, {"t": .76}])
    def extract(src, picks, step, work):
        paths = []
        for index, timestamp in picks:
            path = tmp_path / f"{index}.png"
            candidates[index][1].save(path)
            paths.append(path)
        return paths
    monkeypatch.setattr(vision, "_extract_frames", extract)
    result = vision.analyse_video(tmp_path / "source.mp4", 1.25, tmp_path / "frames")
    assert result.shots[0].frame_times == [.25, .75]
    assert result.shots[0].frame_motions == [10, 20]
    assert [Image.open(p).getpixel((0, 0))[0] for p in result.frames] == [10, 40]


def test_real_cut_time_motion_and_same_name_frames_are_independent(tmp_path):
    a, b = tmp_path / "a/same.mp4", tmp_path / "b/same.mp4"
    a.parent.mkdir()
    b.parent.mkdir()
    subprocess.run([_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=640x480:r=25:d=2",
                    "-f", "lavfi", "-i", "color=blue:s=640x480:r=25:d=2", "-filter_complex",
                    "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-c:v", "libx264", str(a)], check=True)
    subprocess.run([_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=green:s=640x480:r=25:d=4",
                    "-c:v", "libx264", str(b)], check=True)
    first = vision.analyse_video(a, 4, tmp_path / "frames")
    assert first.scene_cuts == [2.0]  # the new blue scene begins at exactly two seconds
    assert all(shot.motion == 0 for shot in first.shots)  # no motion within either static scene
    assert all(len(s.frames) == len(s.frame_times) == len(s.frame_motions) for s in first.shots)
    red, blue = [Image.open(s.frames[0]).getpixel((10, 10)) for s in first.shots]
    assert red[0] > 200 and blue[2] > 200
    saved = {path: path.read_bytes() for path in first.frames}
    second = vision.analyse_video(b, 4, tmp_path / "frames")
    assert not set(first.frames) & set(second.frames)
    assert all(path.read_bytes() == data for path, data in saved.items())


def test_long_static_shot_receives_review_frame_within_budget():
    times = [i / 4 for i in range(120)]
    picks = vision._allocate([0.0] * len(times), times, [], 30, 12)
    assert len(picks) == 2


@pytest.mark.parametrize("budget", [3, 12])
def test_all_detected_shots_receive_frames_even_above_old_cap(budget):
    times = [i / 4 for i in range(80)]
    cuts = [(float(i * 2), 20.0) for i in range(1, 10)]
    picks = vision._allocate([0.0] * 80, times, cuts, 20, budget)
    assert {int(p["t"] // 2) + 1 for p in picks} == set(range(1, 11))
    assert len(picks) <= max(budget, 10)


def test_real_fourteen_shots_are_all_represented_and_sent_for_labels(tmp_path, monkeypatch):
    source = tmp_path / "many-cuts.mp4"
    subprocess.run([_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=640x480:r=25:d=28",
                    "-vf", "drawbox=color=blue:t=fill:enable='mod(floor(t/2),2)'",
                    "-c:v", "libx264", str(source)], check=True)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    batches = []
    def label(system, parts, **kw):
        ids = [int(part["text"].split()[1]) for part in parts if part.get("text", "").startswith("镜头 ")]
        assert ids  # all frames must be labeled; no file-summary fallback
        batches.append(ids)
        return json.dumps({"shots": [{"shot_idx": idx, "description": f"color scene {idx}"} for idx in ids]})
    monkeypatch.setattr(vision, "chat_vision", label)
    result = vision.analyse_with_llm(source, 28, tmp_path / "frames")
    assert result.scene_cuts == [float(i * 2) for i in range(1, 14)]
    assert len(result.shots) == len(result.frames) == 14
    assert batches == [list(range(1, 5)), list(range(5, 9)), list(range(9, 13)), [13, 14]]
    assert result.sampling["target_budget"] == 12 and result.sampling["effective_budget"] == 14
    assert result.sampling["covered_shots"] == 14 and not result.sampling["missing_shots"]
    for shot in result.shots:
        assert shot.description and shot.start <= shot.frame_times[0] < shot.end
        with Image.open(shot.frames[0]) as frame:
            red, _, blue = frame.convert("RGB").getpixel((10, 10))
        assert (red > blue) == bool(shot.idx % 2)
    again = vision.analyse_with_llm(source, 28, tmp_path / "second-run")
    assert again.geometry_reused and again.sampling == result.sampling


def test_real_short_video_with_one_candidate_still_has_a_shot(tmp_path):
    source = tmp_path / "short.mp4"
    subprocess.run([_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=640x480:r=30:d=0.12",
                    "-c:v", "libx264", str(source)], check=True)
    result = vision.analyse_video(source, .12, tmp_path / "frames")
    assert len(result.shots) == len(result.frames) == 1
    assert result.frame_times == [0.0]
    assert result.sampling["candidate_frames"] == result.sampling["covered_shots"] == 1


def test_missing_shot_frames_are_degraded_and_not_cached_as_success(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.touch()
    monkeypatch.setattr(graph, "VISION_ENABLED", True)
    monkeypatch.setattr(graph, "WORK_DIR", tmp_path / "work")
    calls = []
    def incomplete(src, duration, work_dir):
        calls.append(1)
        result = sample_analysis(src, duration, work_dir)
        result.shots[0].end = 2
        result.shots[0].description = result.description = "first scene"
        result.shots.append(vision.Shot(2, 2, 4, 0, 0))
        return result
    monkeypatch.setattr(graph, "analyse_with_llm", incomplete)
    for name in ("first", "second"):
        ctx = graph.RunCtx(name, run_dir=tmp_path / name)
        graph.understand_media({"media_folder": str(tmp_path), "_ctx": ctx,
                               "media": [{"name": source.name, "kind": "video", "duration": 4}]})
        assert any("缺少代表帧" in warning for warning in ctx.warnings)
        assert any("缺少描述" in warning for warning in ctx.warnings)
    assert len(calls) == 2


def test_legacy_context_free_analysis_does_not_cache_missing_labels(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.touch()
    monkeypatch.setattr(graph, "VISION_ENABLED", True)
    monkeypatch.setattr(graph, "WORK_DIR", tmp_path / "work")
    calls = []
    online = False
    def analyse(src, duration, work_dir):
        calls.append(1)
        result = sample_analysis(src, duration, work_dir or tmp_path / "frames")
        result.description = "file summary"
        result.shots[0].description = "real label" if online else ""
        return result
    monkeypatch.setattr(graph, "analyse_with_llm", analyse)
    def run():
        return graph.understand_media({"media_folder": str(tmp_path),
            "media": [{"name": source.name, "kind": "video", "duration": 4}]})
    run()
    run()
    assert len(calls) == 2
    online = True
    run()
    assert run()["media"][0]["shots"][0]["description"] == "real label"
    assert len(calls) == 3


def test_real_representative_pixels_match_recorded_native_frame_time(tmp_path):
    source = tmp_path / "clock.mkv"
    # Every native frame independently encodes its index in its red channel.
    # fps resampling used to shift image content several frames past its label.
    subprocess.run([_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=640x480:r=30:d=2",
                    "-vf", "format=gbrp,geq=r='N*4':g=0:b=0,format=bgr0",
                    "-c:v", "ffv1", str(source)], check=True)
    reference = subprocess.run([_ffmpeg(), "-v", "error", "-i", str(source),
                                "-vf", "format=rgb24,crop=1:1:10:10", "-pix_fmt", "rgb24",
                                "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1"],
                               capture_output=True, check=True).stdout
    assert len(reference) == 60 * 3
    result = vision.analyse_video(source, 2, tmp_path / "frames")
    assert len(result.frames) > 3
    for path, timestamp in zip(result.frames, result.frame_times):
        # Compare against a decoded native pixel, without resampling the
        # reference or relying on the generator's negotiated pixel format.
        expected = reference[round(timestamp * 30) * 3]
        with Image.open(path) as image:
            red = image.convert("RGB").getpixel((10, 10))[0]
        assert abs(red - expected) <= 3, (timestamp, expected, red)


def test_cover_top_three_use_frame_motion_not_average_or_first_frame(tmp_path):
    def clip(name, average, values):
        frames = []
        for i in range(len(values)):
            path = tmp_path / f"{name}-{i}.png"
            Image.new("RGB", (8, 8), "blue").save(path)
            frames.append(str(path))
        return {"name": name, "fps": 30, "shots": [{"idx": 1, "start": 0, "end": 4,
                "motion": average, "frames": frames, "frame_times": [.5 + i for i in range(len(values))],
                "frame_motions": values}]}
    low_peak = clip("high-average", 90, [1, 2, 3])
    high_peak = clip("low-average", 10, [20, 35, 30])
    selected = cover_candidates([low_peak, high_peak])
    assert [c["motion"] for c in selected] == [35, 30, 20]
    assert [c["source_frame"] for c in selected] == [45, 75, 15]
    assert all(c["media"] == "low-average" and c["motion_source"] == "frame" for c in selected)
    assert selected[0]["frame"].endswith("low-average-1.png")
    Path(selected[0]["frame"]).write_bytes(b"broken")
    assert [c["motion"] for c in cover_candidates([low_peak, high_peak])] == [30, 20, 3]
    del low_peak["shots"][0]["frame_motions"]
    fallback = cover_candidates([low_peak])[0]
    assert fallback["motion"] is None and fallback["motion_source"] == "shot-average-fallback"


def test_warm_offline_scan_and_understanding_make_no_ffmpeg_calls(tmp_path, monkeypatch):
    source = tmp_path / "media/clip.mp4"
    source.parent.mkdir()
    subprocess.run([_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=640x480:r=25:d=4",
                    "-c:v", "libx264", str(source)], check=True)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(graph, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(graph, "probe_media", media.probe_media)
    monkeypatch.setattr(graph, "VISION_ENABLED", True)
    monkeypatch.setattr(vision, "chat_vision", lambda *a, **kw: (_ for _ in ()).throw(LLMError("offline")))
    def analyse(name):
        state = {"media_folder": str(source.parent), "copy": "copy",
                 "_ctx": graph.RunCtx(name, run_dir=tmp_path / name)}
        state.update(graph.scan_media(state))
        state.update(graph.understand_media(state))
        return state["media"][0]
    first = analyse("A")
    execute = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("warm A/B must not invoke ffprobe or ffmpeg"))
    second = analyse("B")
    assert second["geometry_reused"] and not second["description"]
    assert len(first["thumbnails"]) == len(second["thumbnails"]) == 3
    assert all(Path(p).is_relative_to(tmp_path / "B") for p in second["thumbnails"])
    assert first["thumbnails"] != second["thumbnails"]
    assert second["shots"][0]["frame_times"] == first["shots"][0]["frame_times"]
    calls = []
    def tracked(*args, **kwargs):
        calls.append(args[0])
        return execute(*args, **kwargs)
    monkeypatch.setattr(subprocess, "run", tracked)
    cached_thumb = next((tmp_path / "work/media_cache/probe/frames").rglob("*.jpg"))
    cached_thumb.write_bytes(b"corrupt thumbnail")
    third = analyse("C")
    assert calls and third["geometry_reused"]  # repair scan cache independently
    before = len(calls)
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    fourth = analyse("D")
    assert len(calls) > before and not fourth["geometry_reused"]
