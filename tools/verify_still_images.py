"""Verify supported animated image inputs stay on their first frame in previews.

uv run --no-sync python tools/verify_still_images.py
"""
import hashlib
import json
import subprocess

from PIL import Image

from cut_agent import graph, render
from cut_agent.config import ROOT
from cut_agent.runctl import new_run_id, write_json_atomic


def main():
    root = ROOT / "work" / "verification" / new_run_id()
    folder = root / "media"
    folder.mkdir(parents=True)
    sources = []
    for name, animated in [("animated.gif", True), ("animated.webp", True),
                           ("animated.png", True), ("still.gif", False)]:
        source = folder / name
        image = Image.new("RGB", (640, 480), "red")
        if animated:
            image.save(source, save_all=True, append_images=[Image.new("RGB", image.size, "blue")],
                       duration=200, loop=0, lossless=True)
        else:
            image.save(source)
        with Image.open(source) as opened:
            assert opened.n_frames == (2 if animated else 1)
            if animated:
                opened.seek(1)
                assert opened.convert("RGB").getpixel((320, 240)) == (0, 0, 255)
        sources.append({"name": name, "animated": animated,
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    state = {"media_folder": str(folder), "copy": "图片按时间线时长定格",
             "_ctx": graph.RunCtx("still-images", run_dir=root,
                                   options={"preview": True, "finishing_llm": False})}
    state.update(graph.scan_media(state))
    state["timeline"] = [{"source": "local", "media": item["name"], "kind": "image", "use_duration": 1.2}
                         for item in state["media"]]
    state.update(graph.write_doc(state))
    state.update(graph.finishing_guide(state))
    preview = state["preview"]
    assert preview["status"] == "ready" and preview["frame_count"] == 120 and preview["duration"] == 4.8
    decoded = subprocess.run([render._ffmpeg(), "-v", "error", "-i", preview["path"],
        "-vf", "format=rgb24,crop=1:1:640:360", "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    assert len(decoded.stdout) == 120 * 3
    pixels = [list(decoded.stdout[n * 3:n * 3 + 3]) for n in range(120)]
    assert all(pixel[0] > 220 and max(pixel[1:]) < 30 for pixel in pixels)
    for card, marker in zip(state["storyboard"]["cards"], preview["markers"]):
        assert not card["frame_missing"]
        with Image.open(root / card["frame"]) as image:
            assert image.convert("RGB").getpixel((320, 240)) == (255, 0, 0)
        assert marker["start"] == card["timeline_in_frame"] / 25
        assert marker["end"] == card["timeline_out_frame"] / 25
    for source in sources:
        assert hashlib.sha256((folder / source["name"]).read_bytes()).hexdigest() == source["sha256"]
    subprocess.run([render._ffmpeg(), "-v", "error", "-y", "-i", preview["path"],
                    "-vf", "select='eq(n,119)'", "-frames:v", "1", str(root / "last_frame.png")], check=True)
    report = {"result": "passed", "sources": sources, "preview": preview, "center_pixels": pixels,
              "scope": "真实 GIF/WebP/APNG 首帧定格与分镜一致，全部 120 帧核对色值，源文件不变；浏览器交互未验收"}
    write_json_atomic(root / "still-image-verification.json", report)
    print(json.dumps({"result": "passed", "images": 4, "preview_frames": 120,
                      "report": str(root / "still-image-verification.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
