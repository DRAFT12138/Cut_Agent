"""Verify all eight EXIF image orientations through real exports and preview.

uv run --no-sync python tools/verify_image_orientation.py
"""
import hashlib
import json
import subprocess

from PIL import Image, ImageDraw

from cut_agent import graph, media
from cut_agent.config import ROOT
from cut_agent.runctl import new_run_id, write_json_atomic


COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
ORDERS = {1: [0, 1, 2, 3], 2: [1, 0, 3, 2], 3: [3, 2, 1, 0], 4: [2, 3, 0, 1],
          5: [0, 2, 1, 3], 6: [2, 0, 3, 1], 7: [3, 1, 2, 0], 8: [1, 3, 0, 2]}


def check(image, orientation):
    image = image.convert("RGB")
    w, h = image.size
    points = [(w // 4, h // 4), (w * 3 // 4, h // 4), (w // 4, h * 3 // 4), (w * 3 // 4, h * 3 // 4)]
    for point, index in zip(points, ORDERS[orientation]):
        assert max(abs(a - b) for a, b in zip(image.getpixel(point), COLORS[index])) < 20


def main():
    root = ROOT / "work" / "verification" / new_run_id()
    folder = root / "media"
    folder.mkdir(parents=True)
    hashes = {}
    for orientation in range(1, 9):
        image = Image.new("RGB", (640, 480))
        draw = ImageDraw.Draw(image)
        for box, color in zip([(0, 0, 319, 239), (320, 0, 639, 239),
                               (0, 240, 319, 479), (320, 240, 639, 479)], COLORS):
            draw.rectangle(box, fill=color)
        exif = Image.Exif()
        exif[274] = orientation
        source = folder / f"orientation_{orientation}.jpg"
        image.save(source, quality=95, exif=exif)
        hashes[source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
    state = {"media_folder": str(folder), "copy": "相机照片的方向与镜像",
             "_ctx": graph.RunCtx("image-orientation", run_dir=root, options={"preview": True, "finishing_llm": False})}
    state.update(graph.scan_media(state))
    state["timeline"] = [{"seq": n, "source": "local", "media": item["name"], "kind": "image",
                          "use_duration": .4, "segment_text": f"方向 {n}"}
                         for n, item in enumerate(state["media"], 1)]
    state.update(graph.write_doc(state))
    state.update(graph.finishing_guide(state))
    assert state["preview"]["status"] == "ready" and state["preview"]["frame_count"] == 80
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-i", state["preview"]["path"],
                    "-vf", "select='not(mod(n,10))'", "-fps_mode", "vfr", str(root / "preview_%02d.png")], check=True)
    verified = []
    for n, (item, card) in enumerate(zip(state["media"], state["storyboard"]["cards"]), 1):
        expected = (480, 640) if n >= 5 else (640, 480)
        assert (item["width"], item["height"]) == expected
        with Image.open(root / card["frame"]) as image:
            assert image.size == expected and image.getexif().get(274, 1) == 1
            check(image, n)
        with Image.open(root / f"preview_{n:02d}.png") as image:
            width = round(expected[0] * 720 / expected[1])
            check(image.crop(((1280 - width) // 2, 0, (1280 + width) // 2, 720)), n)
        assert hashlib.sha256((folder / item["name"]).read_bytes()).hexdigest() == hashes[item["name"]]
        verified.append({"orientation": n, "source": item["name"], "display": item["display_geometry"],
                         "storyboard": card["frame"], "preview_frame": f"preview_{n:02d}.png"})
    report = {"result": "passed", "images": verified, "preview_frames": 80, "source_hashes": hashes,
              "scope": "真实 JPEG 的八种 EXIF 方向、导出和预览像素一致，原文件未修改；浏览器交互未验收"}
    write_json_atomic(root / "image-orientation-verification.json", report)
    print(json.dumps({"result": "passed", "directory": str(root), "images": 8, "preview_frames": 80,
                      "report": str(root / "image-orientation-verification.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
