import json
from pathlib import Path

from PIL import Image
import pytest

from cut_agent import graph, runctl, runs
from cut_agent.craft import critique
from cut_agent.finishing import build_guide, music_cues
from cut_agent.llm import LLMError
from cut_agent.shots import select_shot
from cut_agent.storyboard import build_storyboard


def example(tmp_path):
    frame = tmp_path / "frame.png"
    Image.new("RGB", (30, 30), "blue").save(frame)
    media = [{"name": f"clip{i}.mp4", "kind": "video", "duration": 3, "fps": 25,
              "shots": [{"idx": 1, "start": 0, "end": 3, "motion": i,
                         "description": "城市夜景", "roles": ["高潮"], "frames": [str(frame)], "frame_times": [1]}]}
             for i in range(4)]
    rows = [select_shot({"shot_idx": 1, "role": role, "segment_text": "城市夜景", "intensity": intensity}, m)
            for m, role, intensity in zip(media, ["开头", "发展", "高潮", "收尾"], [2, 3, 5, 2])]
    rows, report = critique(rows, media, auto_fix=False)
    state = {"media_folder": str(tmp_path), "media": media, "timeline": rows,
             "copy": "城市夜景", "critique": report}
    state["storyboard"] = build_storyboard(state, tmp_path / "output")
    return state


def test_alternatives_are_real_and_exclude_selected_shot(tmp_path):
    state = example(tmp_path)
    for row in state["timeline"]:
        assert len(row["alternatives"]) == 3
        assert all(a["media"] != row["media"] for a in row["alternatives"])
        assert all(a["reason"] and a["end"] > a["start"] for a in row["alternatives"])


def test_finishing_anchors_and_music_cues_share_storyboard(tmp_path):
    state = example(tmp_path)
    guide = build_guide(state, "bilibili", {"rows": [{"seq": 1, "color": "统一冷暖", "timecode": "WRONG"}], "titles": ["夜色"]})
    for step, card in zip(guide["steps"], state["storyboard"]["cards"]):
        assert step["anchor"] == card
        assert step["transition"] and step["voice"] and step["rhythm"]
    assert guide["steps"][0]["color_source"] == "model"
    assert guide["steps"][1]["color_source"] == "rule"
    assert guide["music"]["ducking_db"] == -12
    assert guide["music"]["cues"] == music_cues(state["timeline"], state["storyboard"])
    assert guide["music"]["cues"][0]["frame"] == state["storyboard"]["cards"][2]["timeline_in_frame"]
    assert guide["delivery"]["width"] == 1920
    assert len(guide["covers"]) == 3 and guide["checklist"]


def test_visual_only_opening_does_not_consume_narration_number(tmp_path):
    state = example(tmp_path)
    state["timeline"][0]["segment_text"] = ""
    state["storyboard"] = build_storyboard(state, tmp_path / "output")
    guide = build_guide(state, "douyin")
    assert [c["narration_seq"] for c in state["storyboard"]["cards"]] == [None, 1, 2, 3]
    assert "无旁白画面" in guide["steps"][0]["voice"]
    assert "旁白第 1 段" in guide["steps"][1]["voice"]
    assert guide["steps"][1]["anchor"]["timeline_in_frame"] == 75
    from cut_agent.storyboard import markdown
    text = "\n".join(markdown(state["storyboard"]))
    assert "本段无旁白，不占旁白编号" in text and "对齐旁白第 1 段" in text


def test_offline_guide_file_is_complete_and_portable(tmp_path, monkeypatch):
    state = example(tmp_path)
    root = tmp_path / "output"
    state["_ctx"] = graph.RunCtx("test", run_dir=root, options={"finishing_llm": False})
    monkeypatch.setattr(graph, "chat_json", lambda *a, **kw: pytest.fail("offline must not call LLM"))
    state.update(graph.write_doc(state))
    result = graph.finishing_guide(state)
    text = Path(result["guide_path"]).read_text(encoding="utf-8")
    for card in state["storyboard"]["cards"]:
        assert card["source_in_tc"] in text and card["timeline_out_tc"] in text
        assert f"- 取材：{card['media']} #{card['shot_idx']}" in text
        assert f"- 旁白原文：{card['text']}" in text
    assert "[查看粗剪方案与素材目录](粗剪方案.md)" in text
    assert "-12" in text and "- [ ]" in text
    assert all((root / c["frame"]).is_file() for c in result["finishing"]["covers"])
    assert json.loads((root / "plan.json").read_text(encoding="utf-8"))["finishing"] == result["finishing"]


def test_llm_failure_degrades_only_editorial_text(tmp_path, monkeypatch):
    state = example(tmp_path)
    state["_ctx"] = graph.RunCtx("test", run_dir=tmp_path / "out")
    monkeypatch.setattr(graph, "chat_json", lambda *a, **kw: (_ for _ in ()).throw(LLMError("offline")))
    result = graph.finishing_guide(state)
    assert len(result["finishing"]["steps"]) == 4
    assert state["_ctx"].warnings


def test_complete_run_includes_eighth_stage(tmp_runs, fake_llm):
    handle, _ = runs.start_run("unused", "test", options={"finishing_llm": False, "platform": "xiaohongshu"}, sync=True)
    assert runs.STAGE_NAMES[-1] == "finishing_guide"
    meta = runs.status_of(handle.run_id)
    assert meta["status"] == "done" and meta["stages_done"] == 8
    guide = runs.stage_artifact(handle.run_id, "finishing_guide")["finishing"]
    assert guide["delivery"]["height"] == 1440
