"""Offline P1 integration: real sample media + ffmpeg, no external LLM/network.

Run: uv run python tools/verify_p1.py
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from cut_agent import graph, vision
from cut_agent.config import ROOT
from cut_agent.llm import LLMError
from cut_agent.runctl import new_run_id
from cut_agent.shots import select_shot
from cut_agent.media import _ffmpeg


def main():
    dest = ROOT / "work" / "verification" / new_run_id()
    dest.mkdir(parents=True, exist_ok=True)
    hardcut = dest / "hardcut.mp4"
    subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=red:s=640x480:r=25:d=2",
                    "-f", "lavfi", "-i", "color=blue:s=640x480:r=25:d=2",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(hardcut)], check=True)
    state = {"media_folder": str(ROOT / "sample_media"),
             "copy": (ROOT / "sample_copy.txt").read_text(encoding="utf-8")}
    state.update(graph.scan_media(state))
    state["_ctx"] = graph.RunCtx("p1-validation", run_dir=dest)
    with patch.object(graph, "VISION_ENABLED", True), patch.object(
        vision, "chat_vision", side_effect=LLMError("offline validation")
    ):
        state.update(graph.understand_media(state))
    videos = [m for m in state["media"] if m["kind"] == "video"]
    assert len(videos) >= 3, "Generate sample media first: python make_sample.py"
    with patch.object(vision, "chat_vision", side_effect=LLMError("offline validation")):
        cut = vision.analyse_with_llm(hardcut, 4, work_dir=dest / "hardcut_frames")
    assert len(cut.shots) >= 2, "Real hard cut must produce multiple shots"
    assert cut.scene_cuts == [2.0], "The hard-cut anchor must point to the new scene"
    assert all(s.motion == 0 for s in cut.shots), "Static shots must not inherit the hard-cut difference"
    videos.append({**cut.to_dict(), "name": str(hardcut), "kind": "video", "fps": 25})
    many = dest / "many-cuts.mp4"
    subprocess.run([_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=640x480:r=25:d=28",
                    "-vf", "drawbox=color=blue:t=fill:enable='mod(floor(t/2),2)'",
                    "-c:v", "libx264", str(many)], check=True)
    with patch.object(vision, "chat_vision", side_effect=LLMError("offline validation")):
        many_cut = vision.analyse_with_llm(many, 28, work_dir=dest / "many_cut_frames")
    assert len(many_cut.shots) == many_cut.sampling["covered_shots"] == 14
    assert not many_cut.sampling["missing_shots"] and many_cut.sampling["effective_budget"] == 14
    videos.append({**many_cut.to_dict(), "name": str(many), "kind": "video", "fps": 25})
    rows = []
    for item in videos:
        assert item["shots"], item["name"]
        for shot in item["shots"]:
            assert shot["frames"] and all(Path(f).is_file() for f in shot["frames"])
            assert len(shot["frames"]) == len(shot["frame_times"]) == len(shot["frame_motions"])
            row = select_shot({"shot_idx": shot["idx"]}, item)
            assert abs(row["start_offset"] - shot["start"]) <= .05
            assert abs(row["start_offset"] + row["use_duration"] - shot["end"]) <= .05
            rows.append(row)
    for i, row in enumerate(rows):
        row.update(seq=i + 1, segment_text=state["copy"].splitlines()[0],
                   role="开头" if i == 0 else "收尾" if i == len(rows) - 1 else "发展")
    state.update(media=videos, timeline=rows)
    exported = graph.write_doc(state)
    state.update(exported)
    state["_ctx"].options["finishing_llm"] = False
    graph.finishing_guide(state)
    board = exported["storyboard"]
    assert all(not card["frame_missing"] and (dest / card["frame"]).is_file() for card in board["cards"])
    text = (dest / "plan.md").read_text(encoding="utf-8")
    assert all(card["source_in_tc"] in text and card["source_out_tc"] in text for card in board["cards"])
    report = {"videos": len(videos), "shots": len(rows), "boundary_checks": "passed",
              "many_cut_sampling": many_cut.sampling,
              "llm": "offline; semantic quality not evaluated", "rows": rows}
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "p1-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(path), "videos": len(videos), "shots": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
