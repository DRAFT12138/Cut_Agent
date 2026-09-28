"""The exported cut list stays row-aligned after a real offline plan edit."""
from __future__ import annotations

from cut_agent import editing, runctl, runs
from conftest import COPY, SAMPLE_MEDIA


def test_cut_lines_keep_one_tsv_row_per_clip_and_match_storyboard(tmp_runs, fake_llm):
    handle, _ = runs.start_run(str(SAMPLE_MEDIA), COPY,
                               options={"finishing_llm": False}, sync=True)
    root = runctl.run_dir(handle.run_id)
    plan = runctl.read_json(root / "plan.json")
    plan["timeline"][0]["segment_text"] = "第一句\n第二句\t下一格"
    plan["timeline"][0]["note"] = "复核源片\r\n确认动作"
    runctl.write_json_atomic(root / "plan.json", plan)

    updated = editing.rebuild(handle.run_id, expected_revision=0)
    lines = (root / "cut_lines.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(updated["timeline"]) + 1
    header = lines[0].split("\t")
    assert header[:7] == ["文件", "类型", "持续时间", "视频起点", "来源", "对应旁白", "备注"]
    assert header[8:12] == ["源入点", "源出点（不含）", "成片入点", "成片出点（不含）"]
    card = updated["storyboard"]["cards"][0]
    row = lines[1].split("\t")
    assert len(row) == len(header)
    assert row[5] == "第一句 第二句 下一格"
    assert row[6] == "复核源片 确认动作"
    assert row[8:12] == [card["source_in_tc"], card["source_out_tc"],
                         card["timeline_in_tc"], card["timeline_out_tc"]]
    assert all(len(line.split("\t")) == len(header) for line in lines[1:])
