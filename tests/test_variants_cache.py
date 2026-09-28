from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from cut_agent import graph, runctl, runs, vision


def test_media_cache_reuses_frames_without_original_run(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr(graph, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(graph, "VISION_ENABLED", True)
    calls = []
    def analyse(src, duration, work_dir):
        calls.append(src)
        frame = work_dir / "frame.png"
        frame.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (10, 10), "red").save(frame)
        shot = vision.Shot(1, 0, 4, 0, 2, frames=[frame], frame_times=[1], description="real description")
        return SimpleNamespace(scene_cuts=[], shots=[shot], frames=[frame], description="summary")
    monkeypatch.setattr(graph, "analyse_with_llm", analyse)
    def state(run):
        return {"media_folder": str(tmp_path), "media": [{"name": "clip.mp4", "kind": "video", "duration": 4,
                                                        "thumbnails": [str(tmp_path / run / "scan.png")]}],
                "_ctx": graph.RunCtx(run, run_dir=tmp_path / run)}
    first = graph.understand_media(state("A"))
    Path(first["media"][0]["shots"][0]["frames"][0]).unlink()
    second = graph.understand_media(state("B"))
    assert len(calls) == 1
    frame = Path(second["media"][0]["shots"][0]["frames"][0])
    assert frame.is_file() and frame.is_relative_to(tmp_path / "B")
    assert second["media"][0]["thumbnails"] == [str(tmp_path / "B/scan.png")]
    source.write_bytes(b"modified media")
    graph.understand_media(state("C"))
    assert len(calls) == 2


def test_variant_preserves_input_and_records_seed(tmp_runs, fake_llm):
    first, _ = runs.start_run("unused", "same copy", seed=8, sync=True)
    second, _ = runs.start_variant(first.run_id, sync=True)
    assert second.run_id != first.run_id
    a = runctl.read_json(runctl.run_dir(first.run_id) / "input.json")
    b = runctl.read_json(runctl.run_dir(second.run_id) / "input.json")
    assert b["copy"] == a["copy"] and b["media_snapshot"] == a["media_snapshot"]
    assert b["seed"] == 9 and 9 in fake_llm.seeds
    assert b["options"]["comparison_group"] == first.run_id
    assert runs.status_of(second.run_id)["status"] == "done"
    groups = {r["comparison_group"] for r in runs.list_runs()}
    assert groups == {first.run_id}


def test_same_seed_with_fixed_model_responses_reproduces_timeline(tmp_runs, fake_llm):
    a, _ = runs.start_run("unused", "same copy", seed=8, sync=True)
    b, _ = runs.start_variant(a.run_id, seed=8, sync=True)
    assert runs.stage_artifact(a.run_id, "build_timeline")["timeline"] == runs.stage_artifact(b.run_id, "build_timeline")["timeline"]
