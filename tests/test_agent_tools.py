import json

import pytest

from cut_agent import graph
from cut_agent.agent_tools import build_context, load_decisions, media_id
from cut_agent.runctl import RunError


MEDIA = [{"name": "clip.mp4", "kind": "video", "duration": 5, "thumbnails": ["abc_t1.jpg", "abc_t3.jpg"]}]


def decisions():
    return {
        "schema_version": 2,
        "segments": [{"segment_id": "segment_001", "text": "第一段", "duration": 3, "mood": "轻快"}],
        "media_descriptions": {media_id("clip.mp4"): "城市街道"},
        "timeline": [{"segment_id": "segment_001", "media_id": media_id("clip.mp4"),
                      "use_duration": 3, "start_offset": 1}],
        "music": {"mood": "轻快", "primary": {"title": "Agent choice"}, "alternatives": []},
    }


def write_decisions(tmp_path, data=None):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(data or decisions()), encoding="utf-8")
    return path


def test_load_decisions_normalizes_ids_and_validates_contract(tmp_path):
    loaded = load_decisions(write_decisions(tmp_path), MEDIA, "第一段")
    assert loaded["segments"][0]["segment_id"] == "segment_001"
    assert loaded["timeline"][0]["media"] == "clip.mp4"
    assert loaded["timeline"][0]["segment_text"] == "第一段"
    assert loaded["media_descriptions"] == {"clip.mp4": "城市街道"}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data["timeline"][0].update(media_id="media_missing"), "不存在的素材"),
        (lambda data: data["timeline"][0].update(start_offset=4), "超出"),
        (lambda data: data["segments"][0].update(text="改写"), "完整覆盖"),
        (lambda data: data["timeline"].append(dict(data["timeline"][0])), "恰好覆盖"),
        (lambda data: data["timeline"][0].update(media_id="", needs_web=False), "needs_web"),
    ],
)
def test_load_decisions_rejects_unsafe_agent_output(tmp_path, mutate, message):
    data = decisions()
    mutate(data)
    with pytest.raises(RunError, match=message):
        load_decisions(write_decisions(tmp_path, data), MEDIA, "第一段")


def test_agent_decisions_bypass_text_model(tmp_path, monkeypatch):
    supplied = load_decisions(write_decisions(tmp_path), MEDIA, "第一段")
    state = {
        "copy": "第一段",
        "media_folder": "/media",
        "media": [{"name": "clip.mp4", "kind": "video", "duration": 5, "shots": []}],
        "log": [],
        "_ctx": graph.RunCtx("agent", options={"agent_decisions": supplied}),
    }
    monkeypatch.setattr(graph, "chat_json", lambda *args, **kwargs: pytest.fail("LLM API must not be called"))
    monkeypatch.setattr(graph, "analyse_with_llm", lambda *args, **kwargs: pytest.fail("vision API must not be called"))
    understood = graph.understand_media(state)
    segmented = graph.plan_segments(state)
    state.update(segmented)
    timeline = graph.build_timeline(state)
    monkeypatch.setattr(graph, "download_for_mood", lambda *args, **kwargs: [])
    monkeypatch.setattr(graph, "controller", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    music = graph.pick_music(state)
    assert understood["media"][0]["description"] == "城市街道"
    assert segmented["segments"][0]["text"] == "第一段"
    assert timeline["timeline"][0]["media"] == "clip.mp4"
    assert music["music"]["primary"]["title"] == "Agent choice"


def test_build_context_exposes_stable_ids_and_timed_observations(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "scan_media", lambda state: {"media": MEDIA, "log": []})
    context = build_context(str(tmp_path), "旁白")
    item = context["media"][0]
    assert context["schema_version"] == 2
    assert item["media_id"] == media_id("clip.mp4")
    assert item["thumbnail_samples"] == [
        {"path": "abc_t1.jpg", "time": 1.25}, {"path": "abc_t3.jpg", "time": 3.75}]
    assert item["max_use_duration"] == 5
