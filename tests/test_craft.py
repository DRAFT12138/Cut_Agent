from copy import deepcopy

from cut_agent.craft import critique, structure
from cut_agent.shots import select_shot


def fixtures():
    media = [{"name": name, "kind": "video", "duration": 2,
              "shots": [{"idx": 1, "start": 0, "end": 2, "motion": motion}]}
             for name, motion in (("quiet", 0), ("action", 20), ("middle", 5))]
    rows = [select_shot({"shot_idx": 1, "segment_text": "旁白内容保留", "intensity": 3}, media[0])
            for _ in range(3)]
    return media, rows


def test_one_pass_repairs_duplicates_hook_and_ending_without_mutating_input():
    media, rows = fixtures()
    before = deepcopy(rows)
    result, report = critique(rows, media)
    assert rows == before
    assert result[0]["media"] == "action"
    assert result[-1]["media"] == "quiet"
    assert all(a["media"] != b["media"] for a, b in zip(result, result[1:]))
    assert result[0]["segment_text"] == ""
    assert [r["segment_text"] for r in result[1:]] == [r["segment_text"] for r in rows]
    assert all(result[1][key] == value for key, value in before[0].items())
    assert report["repair_passes"] == 1
    assert report["repairs"]
    assert any(w["rule"] == "monotonic_emotion" for w in report["warnings"])


def test_unfixable_duplicate_and_short_narration_remain_visible():
    media, rows = fixtures()
    rows[1]["segment_text"] = "长旁白" * 20
    result, report = critique(rows, media[:1])
    rules = [w["rule"] for w in report["warnings"]]
    assert "adjacent_source" in rules and "narration_shortfall" in rules
    assert result[1]["use_duration"] == 2
    assert result[1]["narration_duration"] > 2


def test_report_only_preserves_user_edit():
    media, rows = fixtures()
    result, report = critique(rows, media, auto_fix=False)
    assert [r["media"] for r in result] == [r["media"] for r in rows]
    assert report["repairs"] == [] and report["repair_passes"] == 0
    assert any(w["rule"] == "opening_hook" for w in report["warnings"])


def test_defaults_are_explicit_and_intensity_is_bounded():
    values = structure([{"text": "one"}, {"role": "高潮", "intensity": 99}, {"intensity": "nan"}])
    assert [x["role"] for x in values] == ["开头", "高潮", "收尾"]
    assert values[0]["role_source"] == "rule"
    assert values[1]["role_source"] == "model"
    assert all(1 <= x["intensity"] <= 5 for x in values)


def test_missing_motion_is_not_treated_as_verified_hook():
    result, report = critique([{"source": "web", "use_duration": 6}], [])
    assert result[0]["source"] == "web"
    assert {w["rule"] for w in report["warnings"]} == {"opening_hook", "quiet_ending"}


def test_existing_hook_moves_entire_row_and_preserves_original_opening():
    media, _ = fixtures()
    rows = [select_shot({"shot_idx": 1, "segment_text": text, "role": "发展"}, media[i])
            for i, text in ((0, "原开场"), (2, "中段"), (1, "钩子旁白"))]
    result, report = critique(rows, media)
    assert len(result) == len(rows)
    assert [r["segment_text"] for r in result] == ["钩子旁白", "原开场", "中段"]
    assert all(result[1][key] == value for key, value in rows[0].items())
    assert report["repairs"][0]["mode"] == "move"
    assert "旁白顺序" in report["repairs"][0]["message"]


def test_unselected_hook_preserves_original_span_even_when_other_rules_conflict():
    item = {"name": "one-file", "kind": "video", "duration": 6,
            "shots": [{"idx": i + 1, "start": i * 2, "end": i * 2 + 2, "motion": motion}
                      for i, motion in enumerate((5, 4, 20))]}
    original = select_shot({"shot_idx": 1, "span": True, "use_duration": 4,
                            "segment_text": "原旁白", "note": "保留备注", "role": "发展"}, item)
    result, report = critique([original], [item])
    assert len(result) == 2 and result[0]["shot_idx"] == 3
    assert result[0]["segment_text"] == ""
    assert all(result[1][key] == value for key, value in original.items())
    assert {w["rule"] for w in report["warnings"]} >= {"adjacent_source", "quiet_ending"}
    assert report["repairs"][0]["mode"] == "insert"


def test_adjacent_and_ending_repairs_rank_role_and_description_before_filename():
    media, _ = fixtures()
    for name, motion, desc, roles in (("a-unrelated", 0, "荒野", []),
                                      ("z-relevant", 0, "城市夜景", ["收尾", "发展"])):
        media.append({"name": name, "kind": "video", "duration": 2,
                      "shots": [{"idx": 1, "start": 0, "end": 2, "motion": motion,
                                 "description": desc, "roles": roles}]})
    rows = [select_shot({"shot_idx": 1, "segment_text": "城市夜景", "role": role}, media[i])
            for i, role in ((1, "开头"), (1, "发展"), (0, "收尾"))]
    result, report = critique(rows, media)
    assert result[1]["media"] == "z-relevant"
    assert "角色匹配" in next(r for r in report["repairs"] if r["rule"] == "adjacent_source")["reason"]
    rows[1] = select_shot({"shot_idx": 1}, media[2])
    rows[2] = select_shot({"shot_idx": 1, "segment_text": "城市夜景", "role": "收尾"}, media[1])
    result, report = critique(rows, media)
    assert result[-1]["media"] == "z-relevant"
    assert next(r for r in report["repairs"] if r["rule"] == "quiet_ending")["score"] > 0


def test_unknown_motion_is_not_zero_or_an_automatic_hook():
    media, _ = fixtures()
    for item in media:
        item["shots"][0].pop("motion")
    rows = [select_shot({"shot_idx": 1, "segment_text": "保留"}, media[0])]
    result, report = critique(rows, media)
    assert len(result) == 1 and not report["repairs"]
    assert {w["rule"] for w in report["warnings"]} == {"opening_hook", "quiet_ending"}


def test_web_stage_keeps_the_single_repair_pass_history():
    from cut_agent import graph
    media, rows = fixtures()
    rows.append({"source": "web", "use_duration": 2, "segment_text": "待补网络画面", "needs_web": True})
    timeline, report = critique(rows, media)
    result = graph.explore_web({"timeline": timeline, "media": media, "critique": report})
    assert result["critique"]["repair_passes"] == 1
    assert result["critique"]["repairs"] == report["repairs"]
    assert result["timeline"][1]["segment_text"] == rows[0]["segment_text"]
