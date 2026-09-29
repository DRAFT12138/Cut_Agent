"""Offline full-delivery smoke test using real sample media and ffmpeg.

Creates a reviewable run with documents, guide, frames and opt-in preview.
LLM replies are fixtures; this does not assess online semantic quality.
"""
import json
from unittest.mock import patch

from cut_agent import graph, runs, runctl, vision
from cut_agent.config import ROOT
from cut_agent.llm import LLMError


def main():
    copy = (ROOT / "sample_copy.txt").read_text(encoding="utf-8")
    width = (len(copy) + 2) // 3
    texts = [copy[i:i + width] for i in range(0, len(copy), width)]
    def chat(system, user, **kwargs):
        if "分镜师" in system:
            return {"segments": [{"text": text, "duration": 8, "role": role, "intensity": intensity}
                                 for text, role, intensity in zip(texts, ["开头", "高潮", "收尾"], [2, 5, 2])]}
        if "时间线编排" in system:
            return {"timeline": [{"media": name, "shot_idx": 1, "segment_text": text}
                                 for name, text in zip(["city_night.mp4", "mountain_day.mp4", "ocean_waves.mp4"], texts)]}
        return {"mood": "验证用", "primary": {}, "alternatives": []}
    with patch.object(graph, "chat_json", chat), patch.object(graph, "VISION_ENABLED", True), \
         patch.object(vision, "chat_vision", side_effect=LLMError("offline verification")), \
         patch.object(graph, "download_for_mood", return_value=[]), \
         patch.object(graph, "controller", side_effect=RuntimeError("offline verification")):
        handle, _ = runs.start_run(str(ROOT / "sample_media"), copy, seed=7,
                                  options={"preview": True, "finishing_llm": False}, sync=True)
    meta = runs.status_of(handle.run_id)
    assert meta["status"] == "done", meta
    root = runctl.run_dir(handle.run_id)
    plan = runctl.read_json(root / "plan.json")
    assert plan["preview"]["status"] == "ready", plan["preview"]
    assert len(plan["finishing"]["steps"]) == len(plan["timeline"])
    assert all(step["anchor"] == card for step, card in zip(plan["finishing"]["steps"], plan["storyboard"]["cards"]))
    assert all((root / card["frame"]).is_file() for card in plan["storyboard"]["cards"])
    assert all(c["motion_source"] == "frame" for c in plan["finishing"]["covers"])
    assert [c["motion"] for c in plan["finishing"]["covers"]] == sorted(
        [round(value, 4) for item in plan["media"] for shot in item.get("shots", [])
         for value in shot.get("frame_motions", [])], reverse=True)[:3]
    print(json.dumps({"run_id": handle.run_id, "run_dir": str(root), "stages": meta["stages_done"],
                      "preview_seconds": plan["preview"]["duration"], "guide_steps": len(plan["finishing"]["steps"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
