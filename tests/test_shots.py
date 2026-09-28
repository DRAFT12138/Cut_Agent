import json
from pathlib import Path

import pytest

from cut_agent import graph, vision
from cut_agent.llm import LLMError
from cut_agent.shots import select_shot, shot_inventory


def media():
    return {"name": "test.mp4", "kind": "video", "duration": 12,
            "shots": [{"idx": i + 1, "start": i * 3, "end": (i + 1) * 3,
                       "description": f"scene {i + 1}", "roles": ["发展"]} for i in range(4)]}


def test_selected_shot_owns_source_bounds():
    row = select_shot({"shot_idx": 2, "start_offset": 8, "use_duration": 100}, media())
    assert (row["start"], row["end"], row["use_duration"]) == (3, 6, 3)
    assert row["shot_description"] == "scene 2"
    assert row["span"] is False


def test_span_stays_contiguous_and_at_most_three_shots():
    row = select_shot({"shot_idx": 1, "span": True, "use_duration": 20}, media())
    assert (row["start"], row["end"], row["shot_end_idx"]) == (0, 9, 3)
    gap = media()
    gap["shots"][1]["start"] = 4
    assert select_shot({"shot_idx": 1, "span": True}, gap)["end"] == 3


@pytest.mark.parametrize("offset", [0, 2.4, 8.8, "bad", float("nan"), float("inf")])
def test_invalid_id_still_lands_at_real_boundary(offset):
    row = select_shot({"shot_idx": 999, "start_offset": offset}, media())
    assert row["start_offset"] in (0, 3, 6, 9)
    assert row["end"] <= 12


def test_legacy_file_offsets_are_bounded():
    item = {"name": "legacy.mp4", "kind": "video", "duration": 2}
    row = select_shot({"start_offset": "oops", "use_duration": 15}, item)
    assert row["use_duration"] == 2
    assert row["shot_idx"] is None
    assert row["start_offset"] == 0


def test_default_shot_selection_preserves_editorial_note():
    original = {"note": "Keep the speaker visible", "start_offset": 3.1}
    row = select_shot(original, media())
    assert row["shot_idx"] == 2 and row["start_offset"] == 3
    assert row["note"].startswith(original["note"])
    assert "已对齐最近镜头起点" in row["note"]
    again = select_shot({**row, "shot_idx": 999}, media())
    assert again["note"] == row["note"]
    assert original == {"note": "Keep the speaker visible", "start_offset": 3.1}


def test_inventory_and_graph_reference_same_shot(monkeypatch):
    seen = []
    def match(system, user, **kw):
        seen.append(user)
        return {"timeline": [{"media": "test.mp4", "shot_idx": 3}]}
    monkeypatch.setattr(graph, "chat_json", match)
    patch = graph.build_timeline({"media": [media()], "segments": [{"text": "narration"}]})
    assert "test.mp4#3" in seen[0] and "scene 3" in seen[0]
    assert patch["timeline"][0]["start_offset"] == 6
    assert patch["timeline"][0]["segment_text"] == "narration"


def test_explicit_web_row_never_matches_empty_name(monkeypatch):
    monkeypatch.setattr(graph, "chat_json", lambda *a, **kw: {"timeline": [{"media": "", "source": "web"}]})
    result = graph.build_timeline({"media": [media()], "segments": []})["timeline"][0]
    assert result["source"] == "web" and result["needs_web"]


def video(tmp_path, count=6):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"test")
    return vision.VideoVision("clip", Path("clip"), count * 3, shots=[
        vision.Shot(i, (i - 1) * 3, i * 3, 0, 5, frames=[frame]) for i in range(1, count + 1)])


def test_shot_labels_batch_four_and_preserve_ids(tmp_path, monkeypatch):
    vv = video(tmp_path)
    calls = []
    def chat(system, parts, **kw):
        text = [p["text"] for p in parts if p["type"] == "text"][1:]
        ids = [int(t.split()[1]) for t in text]
        calls.append(ids)
        return json.dumps({"shots": [{"shot_idx": i, "description": f"scene {i}",
                                      "roles": ["高潮", "unknown"]} for i in ids]})
    monkeypatch.setattr(vision, "chat_vision", chat)
    vision.describe_shots(vv)
    assert calls == [[1, 2, 3, 4], [5, 6]]
    assert all(s.description == f"scene {s.idx}" and s.roles == ["高潮"] for s in vv.shots)
    assert vv.to_dict()["shots"][0]["description"] == "scene 1"


def test_partial_or_failed_labels_do_not_invent_descriptions(tmp_path, monkeypatch):
    vv = video(tmp_path)
    replies = iter(['{"shots":[{"shot_idx":2,"description":"real"},'
                    '{"shot_idx":999,"description":"wrong"}]}', LLMError("offline")])
    def chat(*a, **kw):
        reply = next(replies)
        if isinstance(reply, Exception): raise reply
        return reply
    monkeypatch.setattr(vision, "chat_vision", chat)
    vision.describe_shots(vv)
    assert [s.description for s in vv.shots] == ["", "real", "", "", "", ""]
