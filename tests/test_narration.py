import pytest

from cut_agent import graph
from cut_agent.narration import bind_segments, import_source, timing


def make_audio(path, extension="wav"):
    target = path / f"voice.{extension}"
    target.write_bytes(b"immutable narration")
    return target


@pytest.mark.parametrize("extension", ["wav", "mp3", "m4a"])
def test_import_supported_audio_is_content_addressed(tmp_path, extension, monkeypatch):
    completed = type("Result", (), {"returncode": 0, "stderr": "", "stdout":
        '{"streams":[{"codec_type":"audio","duration":"3","sample_rate":"48000",'
        '"channels":2,"codec_name":"aac"}],"format":{}}'})()
    monkeypatch.setattr("cut_agent.narration.subprocess.run", lambda *args, **kwargs: completed)
    monkeypatch.setattr("cut_agent.narration._ffprobe", lambda: "ffprobe")
    source = import_source(make_audio(tmp_path, extension))
    assert source["duration"] == pytest.approx(3, abs=.05)
    assert len(source["sha256"]) == 64 and source["sample_rate"] > 0


def test_video_audio_track_and_missing_audio(tmp_path, monkeypatch):
    source = make_audio(tmp_path, "mp4")
    completed = type("Result", (), {"returncode": 0, "stderr": "", "stdout":
        '{"streams":[{"codec_type":"audio","duration":"2"}],"format":{}}'})()
    monkeypatch.setattr("cut_agent.narration.subprocess.run", lambda *args, **kwargs: completed)
    monkeypatch.setattr("cut_agent.narration._ffprobe", lambda: "ffprobe")
    assert import_source(source)["duration"] == pytest.approx(2, abs=.1)
    silent = make_audio(tmp_path, "mov")
    completed.stdout = '{"streams":[{"codec_type":"video"}],"format":{"duration":"1"}}'
    with pytest.raises(ValueError, match="不包含旁白音轨"):
        import_source(silent)


def test_word_timing_binds_losslessly_and_exposes_boundaries():
    source = {"duration": 4.0, "sha256": "a" * 64}
    words = [
        {"text": "你好。", "start": .2, "end": 1.0, "confidence": .95},
        {"text": "世界", "start": 1.8, "end": 2.6, "confidence": .8, "emphasis": True},
    ]
    result = timing(source, words)
    assert result["status"] == "ready"
    assert result["words"][0]["boundary"] == "sentence"
    assert result["words"][1]["boundary"] == "breath"
    bound = bind_segments([{"text": "你好。"}, {"text": "世界"}], result)
    assert [(row["narration_start"], row["narration_end"]) for row in bound] == [(0, 1.8), (1.8, 4)]
    assert sum(row["duration"] for row in bound) == 4
    assert bound[0]["preferred_cuts"] == [1.0]
    assert bound[1]["timing_confidence"] == .8


def test_invalid_or_missing_recognition_degrades_explicitly():
    source = {"duration": 2.0}
    assert timing(source, None)["status"] == "unavailable"
    invalid = timing(source, [{"text": "坏", "start": 1, "end": 3, "confidence": 2}])
    assert invalid["status"] == "unavailable" and "无效" in invalid["reason"]
    ready = timing(source, [{"text": "不同", "start": 0, "end": 1, "confidence": .9}])
    with pytest.raises(ValueError, match="不能静默错位"):
        bind_segments([{"text": "原文"}], ready)


def test_planning_uses_real_duration_for_timeline(monkeypatch):
    words = [{"text": "第一段", "start": .1, "end": 1, "confidence": .9},
             {"text": "第二段", "start": 2, "end": 3, "confidence": .9}]
    ctx = graph.RunCtx("run", options={"agent_decisions": {"segments": [
        {"text": "第一段", "duration": 9}, {"text": "第二段", "duration": 9}]},
        "narration_audio": "/voice.wav", "narration_words": words})
    monkeypatch.setattr(graph, "import_narration", lambda path: {"duration": 4, "sha256": "a" * 64})
    planned = graph.plan_segments({"copy": "第一段第二段", "_ctx": ctx})
    assert [row["duration"] for row in planned["segments"]] == [2, 2]
    assert planned["narration"]["status"] == "ready"
    timeline = graph.build_timeline({**planned, "media": [], "log": []})["timeline"]
    assert [row["use_duration"] for row in timeline] == [2, 2]
    assert [(row["narration_start"], row["narration_end"]) for row in timeline] == [(0, 2), (2, 4)]
