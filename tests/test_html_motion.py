from pathlib import Path

import pytest

from cut_agent import html_motion, runctl
from cut_agent.editing import patch_plan


def test_extract_document_adds_offline_csp_and_rejects_active_embeds():
    source = ("```html\n<!doctype html><html><head></head><body><script>"
              "window.__CUT_AGENT_TIME__=0;addEventListener('cutagentframe',()=>{})"
              "</script></body></html>\n```")
    result = html_motion._extract_document(source)
    assert "Content-Security-Policy" in result
    assert "connect-src 'none'" in result
    with pytest.raises(runctl.RunError, match="禁用元素"):
        html_motion._extract_document("<html><body><iframe src='x'></iframe></body></html>")
    with pytest.raises(runctl.RunError, match="完整 HTML"):
        html_motion._extract_document("<div>fragment</div>")
    with pytest.raises(runctl.RunError, match="时间驱动动画"):
        html_motion._extract_document("<html><body>static</body></html>")


def test_generate_passes_adjacent_context_to_llm(monkeypatch):
    seen = {}
    monkeypatch.setattr(html_motion.llm, "chat", lambda system, user, **kwargs:
                        seen.update(system=system, user=user) or
                        "<!doctype html><html><script>window.__CUT_AGENT_TIME__;addEventListener('cutagentframe',()=>{})</script></html>")
    result = html_motion.generate("数字翻页", 1.25, {"segment_text": "之前"}, {"media": "after.mp4"},
                                  copy="整篇文案", resolution="4k")
    assert "cutagentframe" in seen["system"]
    assert all(value in seen["user"] for value in ("之前", "after.mp4", "1.250", "整篇文案", "3840×2160"))
    assert result.startswith("<!doctype html>")


def test_generate_pages_plans_scene_count_then_authors_each_page(monkeypatch):
    monkeypatch.setattr(html_motion.llm, "chat_json", lambda *args, **kwargs: {
        "concept": "从地图推进到数据看板",
        "scenes": [{"duration": 1, "brief": "地图光点移动"},
                   {"duration": 2, "brief": "光点汇聚成数字"}],
    })
    prompts = []
    monkeypatch.setattr(html_motion.llm, "chat", lambda system, user, **kwargs:
                        prompts.append(user) or "<!doctype html><html><script>window.__CUT_AGENT_TIME__;addEventListener('cutagentframe',()=>{})</script></html>")
    plan, pages = html_motion.generate_pages("自主设计", 6, {"media": "a.mp4"}, {"media": "b.mp4"},
                                             copy="从城市到未来", resolution="4k")
    assert len(pages) == 2 == len(prompts)
    assert [scene["duration"] for scene in plan["scenes"]] == [2, 4]
    assert "第 1/2 个画面" in prompts[0] and "地图光点移动" in prompts[0]
    assert "第 2/2 个画面" in prompts[1] and "光点汇聚成数字" in prompts[1]


def test_scene_planner_rejects_more_than_six_pages(monkeypatch):
    monkeypatch.setattr(html_motion.llm, "chat_json", lambda *args, **kwargs: {
        "concept": "too many", "scenes": [{"duration": 1, "brief": str(i)} for i in range(7)]})
    with pytest.raises(runctl.RunError, match="1 到 6"):
        html_motion.plan_scenes("", 7, {"media": "a"}, {"media": "b"}, copy="", resolution="4k")


def test_scene_boundaries_snap_to_continuous_music_beats(monkeypatch):
    monkeypatch.setattr(html_motion.llm, "chat_json", lambda *args, **kwargs: {
        "concept": "跟随鼓点切换", "music_analysis": {"mode": "continuous", "bpm": 120, "beats_per_bar": 4},
        "scenes": [{"duration": 0.9, "beats": 2, "brief": "第一画面"},
                   {"duration": 1.1, "beats": 2, "brief": "第二画面"}],
    })
    plan = html_motion.plan_scenes("", 2, {"media": "a"}, {"media": "b"}, copy="",
                                   resolution="4k", music_mode="continuous", bpm=120,
                                   transition_start=3.25)
    assert plan["music_analysis"]["bpm"] == 120
    assert plan["music_analysis"]["beat_phase"] == 6.5
    assert plan["scenes"][0]["duration"] == pytest.approx(.75)
    assert plan["scenes"][1]["duration"] == pytest.approx(1.25)


def test_separate_transition_music_starts_on_its_first_beat(monkeypatch):
    monkeypatch.setattr(html_motion.llm, "chat_json", lambda *args, **kwargs: {
        "concept": "单独音效", "music_analysis": {"mode": "transition", "bpm": 60},
        "scenes": [{"duration": 1.1, "brief": "一"}, {"duration": .9, "brief": "二"}],
    })
    plan = html_motion.plan_scenes("", 2, {"media": "a"}, {"media": "b"}, copy="",
                                   resolution="4k", music_mode="transition", bpm=60,
                                   transition_start=9.4)
    assert plan["music_analysis"]["beat_phase"] == 0
    assert plan["scenes"][0]["duration"] == pytest.approx(1)


def test_insert_generated_html_row(tmp_path: Path):
    asset = tmp_path / "generated" / "motion-test.html"
    asset.parent.mkdir()
    asset.write_text("<html></html>", encoding="utf-8")
    plan = {"platform": "douyin", "media_folder": str(tmp_path), "media": [],
            "timeline": [], "web_assets": []}
    row = {"source": "generated", "kind": "html", "media": asset.name,
           "local_path": str(asset), "use_duration": 1.5, "start_offset": 0,
           "width": 3840, "height": 2160, "resolution": "4k"}
    result = patch_plan(plan, {"op": "insert_generated", "after": 0, "row_data": row})
    assert result["timeline"][0]["kind"] == "html"
    assert result["timeline"][0]["seq"] == 1


def test_save_is_content_addressed(tmp_path: Path):
    first = html_motion.save(tmp_path, "<html><body>A</body></html>")
    second = html_motion.save(tmp_path, "<html><body>A</body></html>")
    assert first == second
    assert first.parent == tmp_path / "generated"
