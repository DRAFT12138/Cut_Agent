from copy import deepcopy
import hashlib
from pathlib import Path
import subprocess

from PIL import Image
import pytest

from cut_agent import graph, media, runctl, runs, vision, webfetch
from cut_agent.editing import rebuild
from cut_agent.finishing import cover_candidates, build_guide, markdown as guide_markdown
from cut_agent.source_timing import FrameIndex, probe_timing
from cut_agent.storyboard import build_storyboard, frame_index, markdown


def make_vfr(root, offset=0):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "vfr.mkv"
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=black:size=640x480:rate=10:duration=1", "-vf",
                    f"setpts=if(lt(N\\,5)\\,N\\,5+(N-5)*3)+{offset}/TB,"
                    "format=gbrp,geq=r='N*24':g=0:b=0,format=bgr0",
                    "-fps_mode", "vfr", "-c:v", "ffv1", str(source)], check=True)
    return source


@pytest.mark.parametrize("offset", [0, 2])
def test_real_vfr_source_frames_and_covers_use_native_pts(tmp_path, offset):
    source = make_vfr(tmp_path / "media", offset)
    item = media._probe_video(source, source.parent)
    index = FrameIndex(item.source_timing)
    assert index.mode == "vfr" and index.count == 10
    assert item.source_timing["origin_pts"] == offset * 1000
    expected_times = [0, .1, .2, .3, .4, .5, .8, 1.1, 1.4, 1.7]
    assert [index.seconds(n) for n in range(10)] == pytest.approx(expected_times)
    assert item.duration == pytest.approx(1.8)
    assert index.nearest(.65) == 6  # midpoint tie rounds toward the later frame
    assert frame_index(1.1, item.fps) == 11 and index.nearest(1.1) == 7
    vv = vision.analyse_video(source, item.duration, tmp_path / "geometry")
    item_dict = {**vars(item), "shots": [s.to_dict() for s in vv.shots]}
    row = {"media": source.name, "kind": "video", "source": "local", "shot_idx": 1,
           "start_offset": 1.1, "use_duration": .6, "segment_text": "原生帧定位"}
    state = {"media_folder": str(source.parent), "media": [item_dict], "timeline": [row]}
    board = build_storyboard(state, tmp_path / "board")
    card = board["cards"][0]
    assert (card["source_in_frame"], card["source_out_frame"]) == (7, 9)
    assert (card["source_in_tc"], card["source_out_tc"]) == ("00:00:01.100000", "00:00:01.700000")
    assert not card["source_frames_estimated"] and card["source_timecode_mode"] == "pts"
    assert card["timeline_out_frame"] == 15
    covers = cover_candidates([item_dict])
    assert covers
    for cover in covers:
        n = expected_times.index(round(cover["frame_time"], 6))
        assert cover["source_frame"] == n and not cover["source_frames_estimated"]
        with Image.open(cover["frame"]) as image:
            assert image.convert("RGB").getpixel((10, 10))[0] == pytest.approx(n * 24, abs=3)
    state["storyboard"] = board
    guide = build_guide(state, "douyin")
    assert guide["steps"][0]["anchor"] == card
    for text in ("\n".join(markdown(board)), guide_markdown(guide)):
        assert "变帧率" in text and card["source_in_tc"] in text


def test_cfr_index_is_compact_and_video_duration_excludes_audio_tail(tmp_path):
    source = tmp_path / "audio-tail.mp4"
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=red:size=160x90:rate=30000/1001:duration=1", "-f", "lavfi", "-i",
                    "sine=duration=2", "-c:v", "libx264", "-c:a", "aac", str(source)], check=True)
    item = media._probe_video(source, tmp_path)
    record = item.source_timing
    index = FrameIndex(record)
    assert index.mode == "cfr" and index.count == 30 and "pts" not in record
    assert record["step_pts"] > 0
    assert item.duration == pytest.approx(1.001)
    state = {"media_folder": str(tmp_path), "media": [vars(item)],
             "timeline": [{"media": source.name, "start_offset": 0, "use_duration": item.duration}]}
    card = build_storyboard(state, tmp_path / "board")["cards"][0]
    assert card["source_out_frame"] == 30 and card["source_out_tc"] == "00:00:01:00"
    assert not card["source_frames_estimated"]


def test_vfr_cache_reuse_damage_and_offline_rebuild(tmp_runs, fake_llm, tmp_path, monkeypatch):
    source = make_vfr(tmp_path / "media")
    cache = tmp_path / "cache"
    first = media.probe_media(source.parent, cache_dir=cache)[0]
    execute = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("warm scan must reuse timestamps"))
    second = media.probe_media(source.parent, cache_dir=cache)[0]
    assert second.source_timing == first.source_timing
    record_path = next((cache / "probe" / "units" / "probe").glob("*.json"))
    saved = runctl.read_json(record_path)
    # Damage the actual persisted native PTS list, leaving the thumbnail valid.
    saved["source_timing"]["pts"][7] = saved["source_timing"]["pts"][6]
    runctl.write_json_atomic(record_path, saved)
    calls = []
    def track(*args, **kwargs):
        calls.append(args[0])
        return execute(*args, **kwargs)
    monkeypatch.setattr(subprocess, "run", track)
    third = media.probe_media(source.parent, cache_dir=cache)[0]
    assert calls and third.source_timing == first.source_timing
    monkeypatch.setattr(graph, "probe_media", media.probe_media)
    monkeypatch.setattr(graph, "WORK_DIR", tmp_path / "work")
    h, _ = runs.start_run(str(source.parent), "帧定位", sync=True, options={"finishing_llm": False})
    root = runctl.run_dir(h.run_id)
    plan = runctl.read_json(root / "plan.json")
    assert plan["media"][0]["source_timing"] == first.source_timing
    plan["timeline"] = [{"seq": 1, "source": "local", "media": source.name, "kind": "video",
                         "start_offset": 1.1, "use_duration": .6, "segment_text": "离线编辑"}]
    runctl.write_json_atomic(root / "plan.json", plan)
    monkeypatch.setattr(graph, "chat_json", lambda *a, **kw: pytest.fail("rebuild must be offline"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("rebuild must reuse saved timestamps"))
    changed = rebuild(h.run_id)
    assert changed["storyboard"]["cards"][0]["source_in_frame"] == 7
    assert changed["finishing"]["steps"][0]["anchor"] == changed["storyboard"]["cards"][0]


def test_web_video_and_legacy_download_cache_get_native_index(tmp_path, monkeypatch):
    source = make_vfr(tmp_path / "media")
    info = webfetch.inspect_media(source)
    assert FrameIndex(info["source_timing"]).nearest(1.1) == 7
    url = "https://example.test/vfr"
    entry = tmp_path / "cache" / hashlib.sha256(url.encode()).hexdigest()
    entry.mkdir(parents=True)
    original = entry / ("media" + info["extension"])
    original.write_bytes(source.read_bytes())
    metadata = {**info, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    metadata.pop("source_timing")
    runctl.write_json_atomic(entry / "media.json", metadata)
    monkeypatch.setattr(webfetch.requests, "get", lambda *a, **kw: pytest.fail("legacy cache should upgrade locally"))
    asset = webfetch.acquire({"url": url}, tmp_path / "download", tmp_path / "cache")
    assert asset["status"] == "verified"
    assert FrameIndex(asset["source_timing"]).nearest(1.1) == 7
    row = {"source": "web", "kind": "video", "asset_status": "verified", "thumbnail": asset["thumbnail"],
           "source_fps": asset["fps"], "source_timing": asset["source_timing"],
           "start_offset": 1.1, "use_duration": .6}
    board = build_storyboard({"media_folder": str(tmp_path), "timeline": [row]}, tmp_path / "board")
    assert board["cards"][0]["source_in_frame"] == 7


def test_missing_or_invalid_index_is_explicitly_estimated(tmp_path, monkeypatch):
    source = make_vfr(tmp_path / "media")
    item = media._probe_video(source, source.parent)
    record = item.source_timing
    bad = deepcopy(record)
    bad["pts"][7] = bad["pts"][6]
    assert FrameIndex.optional(bad) is None
    bad["time_base"] = "0/0"
    assert FrameIndex.optional(bad) is None
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1, "", "decode failed"))
    timing = probe_timing(source, {}, 1.8)
    assert timing["status"] == "unavailable" and "decode failed" in timing["error"]
    for unavailable in ({}, bad, timing):
        state = {"media_folder": str(source.parent), "media": [{**vars(item), "source_timing": unavailable}],
                 "timeline": [{"media": source.name, "start_offset": 1.1, "use_duration": .6}]}
        card = build_storyboard(state, tmp_path / "board")["cards"][0]
        assert card["source_frames_estimated"] and "估算" in card["source_timing_note"]
