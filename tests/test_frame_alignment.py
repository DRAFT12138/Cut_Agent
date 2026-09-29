from copy import deepcopy
import subprocess

import pytest

from cut_agent import graph, media, render, runctl, runs
from cut_agent.editing import load_plan, rebuild
from cut_agent.source_timing import align_source


def make_clip(folder, mode, origin=0, rate=10, codec="ffv1"):
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / f"{mode}_{origin}_{rate}.mkv"
    formula = "if(lt(N\\,5)\\,N\\,5+(N-5)*3)" if mode == "vfr" else "N"
    codec_options = (["-pix_fmt", "yuv420p", "-x264-params", "scenecut=0:keyint=60:min-keyint=60", "-bf", "3"]
                     if codec == "libx264" else [])
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
        f"color=black:size=160x120:rate={rate}:duration=1", "-vf",
        f"setpts={formula}+{origin}/TB,format=gbrp,"
        "geq=r='mod(N,4)*60':g='floor(N/4)*16':b=0,format=bgr0",
        "-fps_mode", "vfr", "-c:v", codec, *codec_options, str(source)], check=True)
    item = media._probe_video(source, folder)
    return source, {**vars(item), "path": str(item.path)}


def native_frames(path):
    raw = subprocess.run([media._ffmpeg(), "-v", "error", "-i", str(path),
        "-vf", "format=rgb24,crop=1:1:640:360", "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return [round(raw[n] / 60) + 4 * round(raw[n + 1] / 16) for n in range(0, len(raw), 3)]


@pytest.mark.parametrize("mode,origin", [("cfr", 0), ("cfr", 2), ("vfr", 0), ("vfr", 2)])
@pytest.mark.parametrize("fps", [25, 30000 / 1001])
def test_fractional_source_ranges_match_preview_frames(tmp_path, monkeypatch, mode, origin, fps):
    source, item = make_clip(tmp_path / "media", mode, origin)
    starts = [.11, .14, .16] if mode == "cfr" else [.61, .66, .81, 1.01]
    expected = [(1, 5), (1, 5), (2, 6)] if mode == "cfr" else [(5, 7), (6, 7), (6, 7), (7, 8)]
    rows = [{"media": source.name, "source": "local", "kind": "video", "start_offset": start,
             "use_duration": .4, "segment_text": f"row {n}"} for n, start in enumerate(starts)]
    state = {"media_folder": str(source.parent), "media": [item], "timeline": rows,
             "_ctx": graph.RunCtx("alignment", run_dir=tmp_path / "run", options={"finishing_llm": False})}
    original = deepcopy(rows)
    exported = graph.write_doc(state)
    state.update(exported)
    state.update(graph.finishing_guide(state))
    # Standalone consumers share the same range resolution at either output fps.
    board = graph.build_storyboard(state, tmp_path / "board", timeline_fps=fps)
    result = render.render_video(rows, source.parent, {source.name: item}, None, tmp_path / "preview.mp4", fps=fps)
    decoded = native_frames(result["path"])
    assert len(decoded) == board["cards"][-1]["timeline_out_frame"] == result["frame_count"]
    assert rows == original
    for card, marker, (first, end), row in zip(board["cards"], result["markers"], expected, state["timeline"]):
        assert (card["source_in_frame"], card["source_out_frame"]) == (first, end)
        assert row["start_offset"] == pytest.approx(card["source_in_seconds"])
        assert row["use_duration"] == pytest.approx(card["source_out_seconds"] - card["source_in_seconds"])
        assert card["source_alignment"]["requested_duration"] == .4
        assert "实际起点" in card["source_timing_note"]
        frames = decoded[card["timeline_in_frame"]:card["timeline_out_frame"]]
        assert frames[0] == first and frames[-1] == end - 1
        assert all(first <= n < end for n in frames)
        assert frames == sorted(frames)
        assert marker["start"] == card["timeline_in_frame"] / fps
        assert marker["end"] == card["timeline_out_frame"] / fps
    assert all(step["anchor"] == card for step, card in zip(state["finishing"]["steps"], state["storyboard"]["cards"]))
    saved = runctl.read_json(tmp_path / "run" / "plan.json")
    assert saved["timeline"] == state["timeline"]
    monkeypatch.setattr(render, "normalize_segment", lambda *a, **kw: pytest.fail("aligned segments must reuse cache"))
    assert render.render_video(rows, source.parent, {source.name: item}, None, tmp_path / "preview.mp4", fps=fps)["frame_count"] == len(decoded)


@pytest.mark.parametrize("codec", ["ffv1", "libx264"])
def test_high_rate_input_keeps_the_documented_first_frame(tmp_path, monkeypatch, codec):
    source, item = make_clip(tmp_path / "media", "cfr", rate=60, codec=codec)
    commands = []
    original_run = render._run
    def recorded_run(args, timeout):
        commands.append(args)
        return original_run(args, timeout)
    monkeypatch.setattr(render, "_run", recorded_run)
    if codec == "libx264":
        types = subprocess.run([media._ffprobe(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "frame=pict_type", "-of", "csv=p=0", str(source)],
            capture_output=True, text=True, check=True).stdout.splitlines()
        assert "B" in types
    row = {"media": source.name, "source": "local", "kind": "video", "start_offset": .102, "use_duration": .4}
    board = graph.build_storyboard({"media_folder": str(source.parent), "media": [item], "timeline": [row]}, tmp_path / "board")
    result = render.render_video([row], source.parent, {source.name: item}, None, tmp_path / "preview.mp4")
    frames = native_frames(result["path"])
    card = board["cards"][0]
    assert card["source_in_frame"] == 6
    assert frames[0] == card["source_in_frame"]
    assert all(card["source_in_frame"] <= n < card["source_out_frame"] for n in frames)
    assert any("-copyts" in args for args in commands)
    assert not any("trim=start_frame=" in " ".join(args) for args in commands)


def test_seek_pts_mismatch_falls_back_to_ordinal_decode(tmp_path, monkeypatch):
    source, item = make_clip(tmp_path / "media", "cfr", origin=2)
    item["source_timing"]["origin_pts"] += 1
    commands = []
    original_run = render._run
    def recorded_run(args, timeout):
        commands.append(args)
        return original_run(args, timeout)
    monkeypatch.setattr(render, "_run", recorded_run)
    row = {"media": source.name, "source": "local", "kind": "video",
           "start_offset": .61, "use_duration": .2}
    result = render.render_video([row], source.parent, {source.name: item}, None,
                                 tmp_path / "preview.mp4")
    assert native_frames(result["path"])[0] == 6
    assert any("-copyts" in args for args in commands)
    assert any("trim=start_frame=6:" in " ".join(args) for args in commands)


@pytest.mark.parametrize("origin", [0, 2])
def test_legacy_video_without_index_recovers_fractional_preview(tmp_path, monkeypatch, origin):
    source, item = make_clip(tmp_path / "media", "cfr", origin=origin)
    item["source_timing"] = {}
    commands = []
    original_run = render._run
    def recorded_run(args, timeout):
        commands.append(args)
        return original_run(args, timeout)
    monkeypatch.setattr(render, "_run", recorded_run)
    row = {"media": source.name, "source": "local", "kind": "video",
           "start_offset": .11, "use_duration": .4}
    result = render.render_video([row], source.parent, {source.name: item}, None,
                                 tmp_path / "preview.mp4")
    assert len(native_frames(result["path"])) == result["frame_count"] == 10
    assert any("trim=start=0.110000000:end=0.510000000" in " ".join(args) for args in commands)
    card = graph.build_storyboard({"media_folder": str(source.parent), "media": [item],
                                   "timeline": [row]}, tmp_path / "board")["cards"][0]
    assert card["source_frames_estimated"] and "估算" in card["source_timing_note"]


def test_legacy_full_decode_does_not_hide_insufficient_source_frames(tmp_path):
    source, item = make_clip(tmp_path / "media", "cfr")
    item["source_timing"] = {}
    row = {"media": source.name, "source": "local", "kind": "video",
           "start_offset": .8, "use_duration": .4}
    with pytest.raises(render.RenderError, match="需要 10 帧"):
        render.render_video([row], source.parent, {source.name: item}, None,
                            tmp_path / "short.mp4")


def test_subframe_edit_is_persisted_idempotently_without_decoding(tmp_path, tmp_runs, fake_llm, monkeypatch):
    source, item = make_clip(tmp_path / "media", "vfr")
    handle, _ = runs.start_run("unused", "alignment", sync=True)
    plan = load_plan(handle.run_id)
    plan.update(media_folder=str(source.parent), media=[item], timeline=[{"media": source.name,
        "source": "local", "kind": "video", "start_offset": .61, "use_duration": .4}])
    runctl.write_json_atomic(runctl.run_dir(handle.run_id) / "plan.json", plan)
    def forbidden(*a, **kw):
        pytest.fail("offline rebuild must reuse native timing without model or subprocess calls")
    monkeypatch.setattr(graph, "chat_json", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    first = rebuild(handle.run_id)
    row = first["timeline"][0]
    assert row["start_offset"] == .5 and row["use_duration"] == pytest.approx(.6)
    assert row["source_alignment"]["requested_start"] == .61
    second = rebuild(handle.run_id)
    assert second["timeline"] == first["timeline"]
    assert second["storyboard"] == first["storyboard"]
    for stage in ("build_timeline", "explore_web", "write_doc"):
        assert runs.stage_artifact(handle.run_id, stage)["timeline"] == second["timeline"]


def test_web_ranges_and_single_frame_minimum_share_alignment(tmp_path):
    source, item = make_clip(tmp_path / "media", "vfr")
    row = {"source": "web", "kind": "video", "media": "web-clip", "asset_status": "verified",
           "local_path": str(source), "source_fps": item["fps"], "source_timing": item["source_timing"],
           "start_offset": .81, "use_duration": .01}
    resolved = align_source(row)
    assert resolved["start_offset"] == .8 and resolved["use_duration"] == pytest.approx(.3)
    result = render.render_video([row, row], tmp_path, {}, None, tmp_path / "preview.mp4")
    assert result["frame_count"] == 15
    assert native_frames(result["path"]) == [6] * 15
    bad = {**row, "start_offset": 1.7, "use_duration": .5}
    assert align_source(bad)["use_duration"] == .5
    with pytest.raises(render.RenderError, match="超过可解码源帧范围"):
        render.render_video([bad], tmp_path, {}, None, tmp_path / "bad.mp4")
