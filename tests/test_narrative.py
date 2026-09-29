from cut_agent import graph
from cut_agent.narrative import plan


def test_structure_is_lossless_hierarchical_and_actionable():
    copy = "你知道吗？\n数据显示增长 20%；然而风险仍在。最后回到问题。"
    segments, outline = plan(copy, [{"text": "模型改写了文案"}])

    assert "".join(row["text"] for row in segments) == copy
    assert [row["narrative_role"] for row in segments] == ["钩子", "证据", "转折", "收束"]
    assert outline["source_text"] == copy
    assert [chapter["title"] for chapter in outline["chapters"]] == ["开场", "主体", "结尾"]
    assert all("sentence_id" in sentence for row in segments for sentence in row["sentences"])
    assert all({"information_density", "emotion", "visual_goal", "required_entities",
                "acceptable_alternatives"} <= row.keys() for row in segments)


def test_segment_diagnostics_are_deterministic():
    copy = "短。" + "很长的一段" * 20 + "。结尾。"
    first = plan(copy, [])[1]["suggestions"]
    second = plan(copy, [])[1]["suggestions"]
    assert first == second
    assert {item["type"] for item in first} >= {"merge", "split"}


def test_timeline_prompt_receives_whole_film_structure(monkeypatch):
    captured = {}
    monkeypatch.setattr(graph, "chat_json", lambda system, user, **kw: captured.setdefault("user", user) or {"timeline": []})
    state = {"media": [{"name": "a.jpg", "kind": "image", "duration": 0}],
             "segments": [{"text": "开场", "duration": 2, "narrative_role": "钩子", "visual_goal": "人物特写",
                           "required_entities": ["人物"], "emotion": "紧张"}],
             "narrative": {"chapters": [{"title": "开场", "purpose": "建立注意", "segment_ids": ["segment_001"]}]},
             "log": []}
    graph.build_timeline(state)
    assert "全篇结构" in captured["user"] and "建立注意" in captured["user"]
    assert "人物特写" in captured["user"] and "必须实体: 人物" in captured["user"]
