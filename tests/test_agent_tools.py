import json

import pytest

from cut_agent import graph
from cut_agent.agent_tools import build_context, load_decisions
from cut_agent.runctl import RunError


def decisions():
    return {
        "schema_version": 1,
        "segments": [{"text": "第一段", "duration": 3, "mood": "轻快"}],
        "media_descriptions": {"clip.mp4": "城市街道"},
        "timeline": [{"media": "clip.mp4", "segment_text": "第一段", "use_duration": 3}],
        "music": {"mood": "轻快", "primary": {"title": "Agent choice"}, "alternatives": []},
    }


def test_load_decisions_validates_contract(tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(decisions()), encoding="utf-8")
    loaded = load_decisions(path, {"clip.mp4"})
    assert loaded["segments"][0]["text"] == "第一段"
    assert loaded["timeline"][0]["media"] == "clip.mp4"

    bad = decisions()
    bad["timeline"][0]["media"] = "missing.mp4"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(RunError, match="不存在的素材"):
        load_decisions(path, {"clip.mp4"})


def test_agent_decisions_bypass_text_model(monkeypatch):
    supplied = decisions()
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


def test_build_context_exposes_media_and_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "scan_media", lambda state: {
        "media": [{"name": "clip.mp4", "thumbnails": ["frame.jpg"]}], "log": []})
    context = build_context(str(tmp_path), "旁白")
    assert context["schema_version"] == 1
    assert context["media"][0]["thumbnails"] == ["frame.jpg"]
    assert "timeline" in context["decision_contract"]
