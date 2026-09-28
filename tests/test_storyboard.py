import json
from pathlib import Path

from PIL import Image

from cut_agent import graph
from cut_agent.storyboard import build_storyboard, frame_index, timecode, markdown


def state_with_frames(tmp_path):
    frames = []
    for i in range(3):
        path = tmp_path / f"source_{i}.png"
        Image.new("RGB", (20, 20), (i * 80, 0, 0)).save(path)
        frames.append(str(path))
    return {"media_folder": str(tmp_path), "copy": "旁白", "media": [
        {"name": "video.mp4", "kind": "video", "fps": 30, "duration": 6,
         "shots": [{"idx": i + 1, "start": i * 2, "end": i * 2 + 2,
                    "frames": [frames[i]], "frame_times": [i * 2 + 1]} for i in range(3)]}],
        "timeline": [{"media": "video.mp4", "kind": "video", "source": "local",
                      "shot_idx": 2, "start_offset": 2, "use_duration": 2,
                      "segment_text": "较长的一段旁白" * 5, "role": "开头"},
                     {"media": "", "source": "web", "use_duration": 1, "segment_text": "待补"}]}


def test_frame_selection_and_shared_anchors(tmp_path):
    state = state_with_frames(tmp_path)
    output = tmp_path / "output"
    board = build_storyboard(state, output)
    a, b = board["cards"]
    assert (a["source_in_frame"], a["source_out_frame"]) == (60, 120)
    assert a["source_fps"] == 30 and not a["source_fps_assumed"]
    assert (a["timeline_in_frame"], a["timeline_out_frame"], b["timeline_in_frame"]) == (0, 50, 50)
    assert a["shortfall"] > 0
    assert not a["frame_missing"] and b["frame_missing"]
    with Image.open(output / a["frame"]) as im:
        assert im.getpixel((0, 0)) == (80, 0, 0)
    assert board["utilization"]["unused"] == [{"media": "video.mp4", "shot_idx": 1},
                                              {"media": "video.mp4", "shot_idx": 3}]
    text = "\n".join(markdown(board))
    assert a["source_in_tc"] in text and a["timeline_out_tc"] in text
    assert "画面短于旁白" in text


def test_timecode_rollover_and_fractional_fps():
    assert timecode(25 * 60, 25) == "00:01:00:00"
    assert frame_index(.5, 25) == 13
    assert frame_index(10, 30000 / 1001) == 300
    assert timecode(300, 30000 / 1001) == "00:00:10:00"


def test_document_and_json_export_same_storyboard(tmp_path):
    state = state_with_frames(tmp_path)
    output = tmp_path / "run"
    state["_ctx"] = graph.RunCtx("test", run_dir=output)
    patch = graph.write_doc(state)
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    assert plan["storyboard"] == patch["storyboard"]
    text = (output / "plan.md").read_text(encoding="utf-8")
    assert text == (output / "粗剪方案.md").read_text(encoding="utf-8")
    for card in plan["storyboard"]["cards"]:
        assert (output / card["frame"]).is_file()
        assert card["frame"] in text
        assert card["source_in_tc"] in text
        assert card["source_out_tc"] in text


def test_minimal_plan_roundtrip_normalizes_absent_optional_stage_data(tmp_path, monkeypatch):
    state = state_with_frames(tmp_path)
    state["_ctx"] = graph.RunCtx("test", run_dir=tmp_path / "first")
    first = graph.write_doc(state)
    plan = json.loads(Path(first["plan_path"]).read_text(encoding="utf-8"))
    assert plan["music"] == plan["critique"] == {}
    assert plan["segments"] == plan["web_assets"] == []
    # Older minimal exports wrote null for those fields; they remain renderable.
    plan.update(music=None, critique=None, segments=None, web_assets=None)
    plan["_ctx"] = graph.RunCtx("second", run_dir=tmp_path / "second", options={"finishing_llm": False})
    monkeypatch.setattr(graph, "chat_json", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("offline")))
    plan.update(graph.write_doc(plan))
    guide = graph.finishing_guide(plan)["finishing"]
    assert plan["storyboard"] == first["storyboard"]
    assert guide["steps"][0]["anchor"] == plan["storyboard"]["cards"][0]
