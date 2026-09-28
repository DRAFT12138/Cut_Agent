import hashlib
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw
import pytest

from cut_agent import graph, media, render, runctl, webfetch


COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
# Expected top-left, top-right, bottom-left, bottom-right after each EXIF transform.
ORDERS = {1: [0, 1, 2, 3], 2: [1, 0, 3, 2], 3: [3, 2, 1, 0], 4: [2, 3, 0, 1],
          5: [0, 2, 1, 3], 6: [2, 0, 3, 1], 7: [3, 1, 2, 0], 8: [1, 3, 0, 2]}


def photo(path, orientation):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (640, 480))
    draw = ImageDraw.Draw(image)
    for box, color in zip([(0, 0, 319, 239), (320, 0, 639, 239),
                           (0, 240, 319, 479), (320, 240, 639, 479)], COLORS):
        draw.rectangle(box, fill=color)
    exif = Image.Exif()
    exif[274] = orientation
    image.save(path, quality=95, exif=exif)


def check_quadrants(image, orientation):
    image = image.convert("RGB")
    w, h = image.size
    for point, index in zip([(w // 4, h // 4), (w * 3 // 4, h // 4),
                             (w // 4, h * 3 // 4), (w * 3 // 4, h * 3 // 4)], ORDERS[orientation]):
        assert max(abs(a - b) for a, b in zip(image.getpixel(point), COLORS[index])) < 20


@pytest.mark.parametrize("orientation", range(1, 9))
def test_photo_orientation_agrees_in_scan_document_and_preview(tmp_path, monkeypatch, orientation):
    source = tmp_path / "media" / "camera.jpg"
    photo(source, orientation)
    original = source.read_bytes()
    monkeypatch.setattr(graph, "probe_media", media.probe_media)
    monkeypatch.setattr(graph, "WORK_DIR", tmp_path / "work")
    root = tmp_path / "run"
    state = {"media_folder": str(source.parent), "copy": "照片方向",
             "_ctx": graph.RunCtx("photo", run_dir=root, options={"finishing_llm": False})}
    state.update(graph.scan_media(state))
    item = state["media"][0]
    expected = (480, 640) if orientation >= 5 else (640, 480)
    assert (item["width"], item["height"]) == expected
    assert item["display_geometry"]["exif_orientation"] == orientation
    state["timeline"] = [{"media": source.name, "source": "local", "kind": "image", "use_duration": .4}]
    state.update(graph.write_doc(state))
    state.update(graph.finishing_guide(state))
    card = state["storyboard"]["cards"][0]
    for path in [Path(item["thumbnails"][0]), root / card["frame"]]:
        with Image.open(path) as image:
            assert image.getexif().get(274, 1) == 1  # Pixels already oriented; no second transform.
            assert image.width / image.height == pytest.approx(expected[0] / expected[1])
            check_quadrants(image, orientation)
    # Re-export the saved plan; already-normalized assets must not turn a second time.
    saved = runctl.read_json(root / "plan.json")
    saved["_ctx"] = graph.RunCtx("again", run_dir=tmp_path / "again", options={"finishing_llm": False})
    again = graph.write_doc(saved)
    with Image.open(tmp_path / "again" / again["storyboard"]["cards"][0]["frame"]) as image:
        check_quadrants(image, orientation)
    output = tmp_path / "preview.mp4"
    render.render_video(state["timeline"], source.parent, {}, None, output)
    frame = tmp_path / "preview.png"
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-i", str(output), "-frames:v", "1", str(frame)], check=True)
    with Image.open(frame) as image:
        width = round(expected[0] * 720 / expected[1])
        check_quadrants(image.crop(((1280 - width) // 2, 0, (1280 + width) // 2, 720)), orientation)
    assert source.read_bytes() == original


def test_image_probe_cache_and_legacy_web_cache_keep_display_orientation(tmp_path, monkeypatch):
    source = tmp_path / "media" / "camera.jpg"
    photo(source, 7)  # Includes reflection as well as rotation.
    original = source.read_bytes()
    cache = tmp_path / "probe_cache"
    first = media.probe_media(source.parent, cache_dir=cache)[0]
    monkeypatch.setattr(media, "_probe_image", lambda *a, **kw: pytest.fail("warm image scan should reuse cache"))
    second = media.probe_media(source.parent, cache_dir=cache)[0]
    assert second.display_geometry == first.display_geometry and second.width == 480
    url = "https://example.test/photo"
    entry = tmp_path / "web_cache" / hashlib.sha256(url.encode()).hexdigest()
    entry.mkdir(parents=True)
    (entry / "media.jpg").write_bytes(original)
    runctl.write_json_atomic(entry / "media.json", {"kind": "image", "width": 640, "height": 480,
        "duration": 0, "fps": 0, "extension": ".jpg", "sha256": hashlib.sha256(original).hexdigest()})
    monkeypatch.setattr(webfetch.requests, "get", lambda *a, **kw: pytest.fail("legacy cache should upgrade locally"))
    result = webfetch.acquire({"url": url}, tmp_path / "web", tmp_path / "web_cache")
    assert result["status"] == "verified" and (result["width"], result["height"]) == (480, 640)
    assert Path(result["local"]).read_bytes() == original
    with Image.open(result["thumbnail"]) as image:
        assert image.size == (480, 640) and image.getexif().get(274, 1) == 1
        check_quadrants(image, 7)
