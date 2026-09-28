from copy import deepcopy

import pytest

from cut_agent import graph, runctl, runs
from cut_agent.delivery import PLATFORMS, build_delivery
from cut_agent.editing import load_plan, rebuild
from cut_agent.finishing import build_guide, markdown
from cut_agent.storyboard import build_storyboard


def state(seconds=60):
    return {"media_folder": "unused", "media": [{"name": "landscape", "kind": "image", "width": 1920, "height": 1080}],
            "timeline": [{"seq": 1, "media": "landscape", "kind": "image", "source": "local",
                          "use_duration": seconds, "segment_text": "原文", "role": "收尾"}]}


@pytest.mark.parametrize("platform", PLATFORMS)
def test_project_limits_do_not_claim_verified_account_upload_limits(platform):
    sample = state(PLATFORMS[platform]["target_max_seconds"])
    original = deepcopy(sample)
    result = build_delivery(sample, platform)
    assert result["checks"][0]["status"] == "pass"
    assert result["upload_limit"]["max_seconds"] is None
    assert result["upload_limit"]["status"] == "unverified"
    assert result["upload_limit"]["references_checked_at"] == "2026-09-13"
    assert all(ref["scope"] and ref["url"].startswith("https://") for ref in result["upload_limit"]["references"])
    assert sample == original
    if platform == "douyin":
        assert result["upload_limit"]["references"][0]["max_seconds"] == 900
        assert result["target_max_seconds"] == 60


def test_duration_uses_exported_frame_anchor_and_reports_overrun(tmp_path):
    sample = state(60.041)
    sample["storyboard"] = build_storyboard(sample, tmp_path)
    result = build_delivery(sample, "douyin")
    assert result["duration_seconds"] == 60.04
    check = result["checks"][0]
    assert check["status"] == "review" and check["over_seconds"] == .04
    sample["storyboard"]["timeline_fps"] = 30
    assert any(c["code"] == "timeline_fps" for c in build_delivery(sample, "douyin")["checks"])


def test_safe_area_and_source_geometry_are_actionable_without_reframing():
    sample = state()
    result = build_delivery(sample, "douyin")
    assert result["subtitle_safe_rect_px"] == [87, 192, 993, 1536]
    assert {c["code"] for c in result["checks"]} >= {"aspect_ratio", "upscale", "upload_limit"}
    wide = build_delivery(sample, "bilibili")
    assert not {"aspect_ratio", "upscale"} & {c["code"] for c in wide["checks"]}
    sample["media"][0].pop("width")
    assert any(c["code"] == "source_dimensions" for c in build_delivery(sample, "bilibili")["checks"])


def test_guide_markdown_and_checklist_include_same_delivery_warnings(tmp_path):
    sample = state(61)
    sample["storyboard"] = build_storyboard(sample, tmp_path)
    guide = build_guide(sample, "douyin")
    text = markdown(guide)
    checklist = [entry["text"] for entry in guide["checklist"]]
    for check in guide["delivery"]["checks"]:
        assert check["message"] in text
        if check["status"] != "pass":
            assert check["message"] in checklist
    assert "开放平台上传接口" in text and "2026-09-13" in text


def test_invalid_platform_edit_cannot_silently_use_another_preset(tmp_runs, fake_llm, monkeypatch):
    handle, _ = runs.start_run("unused", "sample", sync=True)
    root = runctl.run_dir(handle.run_id)
    plan = load_plan(handle.run_id)
    plan["platform"] = "unknown-platform"
    runctl.write_json_atomic(root / "plan.json", plan)
    before = (root / "plan.md").read_bytes()
    monkeypatch.setattr(graph, "write_doc", lambda *a: pytest.fail("must validate before rendering"))
    with pytest.raises(runctl.RunError, match="平台"):
        rebuild(handle.run_id)
    assert (root / "plan.md").read_bytes() == before
