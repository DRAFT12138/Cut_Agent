"""Real VFR frame/cover/document verification without model calls.

uv run --no-sync python tools/verify_source_timing.py
"""
import json
from pathlib import Path
import subprocess

from PIL import Image

from cut_agent import graph, media, vision, render
from cut_agent.config import ROOT
from cut_agent.runctl import new_run_id, read_json, write_json_atomic
from cut_agent.source_timing import FrameIndex


def main():
    root = ROOT / "work" / "verification" / new_run_id()
    source_dir = root / "media"
    source_dir.mkdir(parents=True)
    font = Path("C:/Windows/Fonts/msyh.ttc")
    font_arg = f"fontfile={render._filter_value(font.as_posix())}:" if font.is_file() else ""
    for offset in (0, 2):
        source = source_dir / f"vfr_offset_{offset}.mkv"
        subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=black:size=640x480:rate=10:duration=1", "-vf",
                        f"setpts=if(lt(N\\,5)\\,N\\,5+(N-5)*3)+{offset}/TB,"
                        "format=gbrp,geq=r='N*24':g=0:b=0,format=bgr0,"
                        f"drawtext={font_arg}text='FRAME %{{n}}':fontsize=40:fontcolor=white:x=50:y=50",
                        "-fps_mode", "vfr", "-c:v", "ffv1", str(source)], check=True)
    ctx = graph.RunCtx("source-timing-verification", run_dir=root,
                       options={"preview": True, "finishing_llm": False})
    state = {"media_folder": str(source_dir), "copy": "变帧率源帧定位验证", "_ctx": ctx}
    state.update(graph.scan_media(state))
    rows, checked_frames = [], []
    native_times = [0, .1, .2, .3, .4, .5, .8, 1.1, 1.4, 1.7]
    for item in state["media"]:
        index = FrameIndex(item["source_timing"])
        assert index.mode == "vfr" and index.count == 10
        assert index.nearest(1.1) == 7
        geometry = vision.analyse_video(source_dir / item["name"], item["duration"], root / "geometry")
        item["shots"] = [s.to_dict() for s in geometry.shots]
        for path, moment in zip(geometry.frames, geometry.frame_times):
            native_frame = native_times.index(round(moment, 6))
            assert index.nearest(moment, allow_end=False) == native_frame
            with Image.open(path) as image:
                assert abs(image.convert("RGB").getpixel((10, 10))[0] - native_frame * 24) <= 3
            checked_frames.append({"source": item["name"], "time": moment, "frame": native_frame,
                                   "image": str(path), "old_fps_derived_frame": round(moment * item["fps"])})
        rows.append({"seq": len(rows) + 1, "source": "local", "kind": "video", "media": item["name"],
                     "start_offset": 1.1, "use_duration": .6, "segment_text": "原生第 7 帧到第 9 帧前", "role": "发展"})
    state["timeline"] = rows
    assert any(f["frame"] != f["old_fps_derived_frame"] for f in checked_frames)
    state.update(graph.write_doc(state))
    state.update(graph.finishing_guide(state))
    plan = read_json(root / "plan.json")
    assert plan["preview"]["status"] == "ready", plan["preview"]
    assert plan["preview"]["frame_count"] == 30
    for card, step in zip(plan["storyboard"]["cards"], plan["finishing"]["steps"]):
        assert (card["source_in_frame"], card["source_out_frame"]) == (7, 9)
        assert (card["source_in_tc"], card["source_out_tc"]) == ("00:00:01.100000", "00:00:01.700000")
        assert step["anchor"] == card
        assert (root / card["frame"]).is_file() and not card["frame_missing"]
    for cover in plan["finishing"]["covers"]:
        with Image.open(root / cover["frame"]) as image:
            assert abs(image.convert("RGB").getpixel((10, 10))[0] - cover["source_frame"] * 24) <= 3
    # Saved indices are self-contained. Rebuild exports with subprocess/model use forbidden.
    def forbidden(*args, **kwargs):
        raise AssertionError("offline export must reuse source timestamps")
    original_run, original_chat = subprocess.run, graph.chat_json
    try:
        subprocess.run = graph.chat_json = forbidden
        copied = dict(plan, _ctx=graph.RunCtx("offline", run_dir=root / "offline", options={"finishing_llm": False}))
        copied.update(graph.write_doc(copied))
        copied.update(graph.finishing_guide(copied))
        assert copied["storyboard"]["cards"] == plan["storyboard"]["cards"]
    finally:
        subprocess.run, graph.chat_json = original_run, original_chat
    report = {"result": "passed", "cards": plan["storyboard"]["cards"], "covers": plan["finishing"]["covers"],
              "checked_native_frames": checked_frames,
              "preview_frames": 30, "sources": [{"name": m["name"], "timing": m["source_timing"]} for m in plan["media"]],
              "scope": "真实变帧率/非零起始时间、源帧像素、同源执行卡/指导及离线导出；未验收浏览器"}
    write_json_atomic(root / "source-timing-verification.json", report)
    print(json.dumps({"result": "passed", "directory": str(root), "preview_frames": 30,
                      "report": str(root / "source-timing-verification.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
