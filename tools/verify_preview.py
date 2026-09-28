"""Real media/ffmpeg P5 smoke test; no LLM or network. uv run python tools/verify_preview.py"""
import json
import subprocess
from pathlib import Path

from cut_agent import graph, render
from cut_agent.config import ROOT
from cut_agent.runctl import new_run_id, write_json_atomic


def main():
    root = ROOT / "work" / "verification" / new_run_id()
    state = {"media_folder": str(ROOT / "sample_media"), "copy": "预览验证",
             "media": [], "timeline": [
                 {"seq": 1, "media": "city_night.mp4", "kind": "video", "source": "local", "use_duration": 1.2, "start_offset": 0},
                 {"seq": 2, "media": "forest.jpg", "kind": "image", "source": "local", "use_duration": 1.2},
                 {"seq": 3, "media": "待补网络素材", "kind": "video", "source": "web", "needs_web": True, "use_duration": 1.2}],
             "_ctx": graph.RunCtx("preview-test", run_dir=root, options={"preview": True})}
    result = graph.write_doc(state)
    state.update(result)
    state["_ctx"].options["finishing_llm"] = False
    graph.finishing_guide(state)
    preview = result["preview"]
    assert preview["status"] == "ready", preview
    assert preview["n_segments"] == 3 and preview["markers"][-1]["missing"]
    board = result["storyboard"]
    expected_frames = board["cards"][-1]["timeline_out_frame"]
    assert preview["frame_count"] == expected_frames == 90
    assert abs(preview["duration"] - 3.6) < .001
    probe = subprocess.run([render._ffprobe(), "-v", "error", "-select_streams", "v:0",
                            "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
                            "-of", "json", preview["path"]], capture_output=True, text=True, check=True)
    timestamps = [float(frame["best_effort_timestamp_time"]) for frame in json.loads(probe.stdout)["frames"]]
    assert len(timestamps) == expected_frames
    assert all(abs(t - i / board["timeline_fps"]) < .0001 for i, t in enumerate(timestamps))
    for card, marker in zip(board["cards"], preview["markers"]):
        assert marker["start"] == card["timeline_in_frame"] / board["timeline_fps"]
        assert marker["end"] == card["timeline_out_frame"] / board["timeline_fps"]
    screenshot = root / "preview_check.png"
    subprocess.run([render._ffmpeg(), "-v", "error", "-y", "-i", preview["path"],
                    "-frames:v", "1", str(screenshot)], check=True)
    report = {"preview": preview, "frame_timestamps": timestamps,
              "storyboard": board, "screenshot": str(screenshot),
              "scope": "真实编码、解码时间戳及执行卡/标记对齐；不代表浏览器播放或主观音量验收"}
    write_json_atomic(root / "preview-verification.json", report)
    print(json.dumps({"preview": preview["path"], "duration": preview["duration"],
                      "frame_count": expected_frames, "screenshot": str(screenshot),
                      "report": str(root / "preview-verification.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
