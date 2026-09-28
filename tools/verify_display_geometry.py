"""Real portrait/rotation/pixel-aspect verification; no model or network.

uv run --no-sync python tools/verify_display_geometry.py
"""
import json
from pathlib import Path
import subprocess

from PIL import Image

from cut_agent import graph, media, vision
from cut_agent.config import ROOT
from cut_agent.runctl import new_run_id, read_json, write_json_atomic


def main():
    root = ROOT / "work" / "verification" / new_run_id()
    folder = root / "media"
    folder.mkdir(parents=True)
    specifications = [("landscape.mp4", "1280x720", "1/1"),
                      ("portrait.mp4", "720x1280", "1/1"),
                      ("anamorphic.mp4", "640x480", "4/3")]
    for name, size, sar in specifications:
        subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=red:size={size}:rate=25:duration=0.4", "-vf",
                        f"drawbox=x=0:y=ih/2:w=iw:h=ih/2:color=blue:t=fill,setsar={sar}",
                        "-c:v", "libx264", str(folder / name)], check=True)
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-display_rotation", "90",
                    "-i", str(folder / "landscape.mp4"), "-c", "copy", str(folder / "rotated.mp4")], check=True)
    state = {"media_folder": str(folder), "copy": "画面方向与比例验收",
             "_ctx": graph.RunCtx("geometry", run_dir=root, options={"preview": True, "finishing_llm": False})}
    state.update(graph.scan_media(state))
    expected = {"landscape.mp4": (1280, 720), "portrait.mp4": (720, 1280),
                "rotated.mp4": (720, 1280), "anamorphic.mp4": (853, 480)}
    rows, checked = [], []
    for item in state["media"]:
        size = expected[item["name"]]
        assert (item["width"], item["height"]) == size
        geometry = vision.analyse_video(folder / item["name"], item["duration"], root / "geometry")
        item["shots"] = [s.to_dict() for s in geometry.shots]
        for frame in geometry.frames:
            with Image.open(frame) as image:
                assert max(image.size) <= vision.FRAME_LONG_EDGE
                assert abs(image.width / image.height - size[0] / size[1]) < .005
                points = [(image.width // 4, image.height // 2), (image.width * 3 // 4, image.height // 2)] if item["name"] == "rotated.mp4" else [(image.width // 2, image.height // 4), (image.width // 2, image.height * 3 // 4)]
                assert image.getpixel(points[0])[0] > 220 and image.getpixel(points[1])[2] > 220
                checked.append({"source": item["name"], "display": item["display_geometry"],
                                "representative": str(frame), "frame_size": image.size})
        rows.append({"seq": len(rows) + 1, "source": "local", "kind": "video", "media": item["name"],
                     "start_offset": 0, "use_duration": .4, "segment_text": item["name"], "role": "发展"})
    state["timeline"] = rows
    state.update(graph.write_doc(state))
    state.update(graph.finishing_guide(state))
    plan = read_json(root / "plan.json")
    assert plan["preview"]["status"] == "ready" and plan["preview"]["frame_count"] == 40
    portrait_rows = {r["seq"] for r in rows if r["media"] in ("portrait.mp4", "rotated.mp4")}
    assert not any(c["code"] == "aspect_ratio" and c.get("seq") in portrait_rows
                   for c in plan["finishing"]["delivery"]["checks"])
    for card in plan["storyboard"]["cards"]:
        with Image.open(root / card["frame"]) as image:
            size = expected[card["media"]]
            assert abs(image.width / image.height - size[0] / size[1]) < .005
    report = {"result": "passed", "checked": checked, "cards": plan["storyboard"]["cards"],
              "delivery": plan["finishing"]["delivery"], "preview_frames": 40,
              "scope": "真实媒体方向/比例/代表帧/分镜及编码；四种旋转角预览像素另有测试，不代表浏览器验收"}
    write_json_atomic(root / "display-geometry-verification.json", report)
    print(json.dumps({"result": "passed", "directory": str(root), "sources": len(expected),
                      "preview_frames": 40, "report": str(root / "display-geometry-verification.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
