import hashlib
import subprocess
from pathlib import Path

from PIL import Image
import pytest

from cut_agent import graph, media, render, runctl, vision, webfetch
from cut_agent.delivery import build_delivery
from cut_agent.storyboard import build_storyboard


def make_video(root, size="1280x720", rotation=0, sar="1/1"):
    root.mkdir(parents=True, exist_ok=True)
    base = root / "base.mp4"
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=red:size={size}:rate=25:duration=0.4", "-vf",
                    f"drawbox=x=0:y=ih/2:w=iw:h=ih/2:color=blue:t=fill,setsar={sar}",
                    "-c:v", "libx264", str(base)], check=True)
    if rotation:
        rotated = root / "rotated.mp4"
        subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-display_rotation", str(rotation),
                        "-i", str(base), "-c", "copy", str(rotated)], check=True)
        return rotated
    return base


def assert_direction(image, rotation):
    image = image.convert("RGB")
    w, h = image.size
    points = ((w // 4, h // 2), (w * 3 // 4, h // 2)) if rotation % 180 else ((w // 2, h // 4), (w // 2, h * 3 // 4))
    colors = (0, 2) if rotation in (0, 90) else (2, 0)
    for point, channel in zip(points, colors):
        pixel = image.getpixel(point)
        assert pixel[channel] > 220 and pixel[2 - channel] < 30


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotation_matches_scan_representatives_storyboard_and_preview(tmp_path, monkeypatch, rotation):
    source = make_video(tmp_path / "media", rotation=rotation)
    monkeypatch.setattr(graph, "probe_media", media.probe_media)
    monkeypatch.setattr(graph, "WORK_DIR", tmp_path / "work")
    ctx = graph.RunCtx("display", run_dir=tmp_path / "run")
    state = {"media_folder": str(source.parent), "_ctx": ctx}
    state.update(graph.scan_media(state))
    item = next(m for m in state["media"] if m["name"] == source.name)
    expected = (720, 1280) if rotation % 180 else (1280, 720)
    assert (item["width"], item["height"]) == expected
    assert (item["display_geometry"]["encoded_width"], item["display_geometry"]["encoded_height"]) == (1280, 720)
    assert item["display_geometry"]["rotation"] % 360 == rotation
    vv = vision.analyse_video(source, item["duration"], tmp_path / "geometry")
    item["shots"] = [s.to_dict() for s in vv.shots]
    for path in [*item["thumbnails"], *[str(p) for p in vv.frames]]:
        with Image.open(path) as image:
            assert max(image.size) <= 640
            assert image.width / image.height == pytest.approx(expected[0] / expected[1], abs=.005)
            assert_direction(image, rotation)
    row = {"media": source.name, "kind": "video", "source": "local", "start_offset": 0, "use_duration": .4}
    state["timeline"] = [row]
    board = build_storyboard(state, ctx.run_dir)
    with Image.open(ctx.run_dir / board["cards"][0]["frame"]) as image:
        assert_direction(image, rotation)
    target = "douyin" if rotation % 180 else "bilibili"
    assert not any(c["code"] in ("aspect_ratio", "source_geometry") for c in build_delivery(state, target)["checks"])
    output = tmp_path / "preview.mp4"
    result = render.render_video([row], source.parent, {}, None, output)
    assert result["frame_count"] == 10
    frame = tmp_path / "preview.png"
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-i", str(output), "-frames:v", "1", str(frame)], check=True)
    with Image.open(frame) as image:
        # Crop the non-black video region; the label is restricted to the top edge.
        pixels = image.convert("RGB")
        xs = [x for x in range(1280) if max(pixels.getpixel((x, 360))[::2]) > 100]
        ys = [y for y in range(720) if max(pixels.getpixel((640, y))[::2]) > 100]
        content = image.crop((min(xs), min(ys), max(xs) + 1, max(ys) + 1))
        assert content.width / content.height == pytest.approx(expected[0] / expected[1], abs=.005)
        assert_direction(content, rotation)


def test_native_portrait_respects_configured_long_edge(tmp_path, monkeypatch):
    source = make_video(tmp_path / "media", size="720x1280")
    monkeypatch.setattr(vision, "FRAME_LONG_EDGE", 320)
    vv = vision.analyse_video(source, .4, tmp_path / "geometry")
    assert vv.frames
    for path in vv.frames:
        with Image.open(path) as image:
            assert image.size == (180, 320)
            assert_direction(image, 0)


def test_anamorphic_display_is_square_pixel_in_frames_and_preview(tmp_path):
    source = make_video(tmp_path / "media", size="640x480", sar="4/3")
    item = media._probe_video(source, source.parent)
    assert (item.width, item.height) == (853, 480)
    assert item.display_geometry["sample_aspect_ratio"] == "4/3"
    vv = vision.analyse_video(source, .4, tmp_path / "geometry")
    with Image.open(vv.frames[0]) as image:
        assert image.size == (640, 360)
    state = {"media": [vars(item)], "timeline": [{"media": source.name, "kind": "video", "use_duration": .4}]}
    assert not any(c["code"] == "aspect_ratio" for c in build_delivery(state, "bilibili")["checks"])
    preview = tmp_path / "preview.mp4"
    render.render_video(state["timeline"], source.parent, {}, None, preview)
    frame = tmp_path / "preview.png"
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-i", str(preview), "-frames:v", "1", str(frame)], check=True)
    with Image.open(frame) as image:
        # Correct 16:9 normalization fills the canvas; old SAR-dropping code had side bars.
        assert image.getpixel((5, 540))[2] > 220 and image.getpixel((1274, 540))[2] > 220


def test_web_rotation_cache_upgrades_without_redownload(tmp_path, monkeypatch):
    source = make_video(tmp_path / "media", rotation=90)
    metadata = webfetch.inspect_media(source)
    assert (metadata["width"], metadata["height"]) == (720, 1280)
    url = "https://example.test/rotated"
    cache = tmp_path / "cache"
    entry = cache / hashlib.sha256(url.encode()).hexdigest()
    entry.mkdir(parents=True)
    (entry / "media.mp4").write_bytes(source.read_bytes())
    metadata.update(sha256=hashlib.sha256(source.read_bytes()).hexdigest(), width=1280, height=720)
    metadata.pop("display_geometry")
    runctl.write_json_atomic(entry / "media.json", metadata)
    monkeypatch.setattr(webfetch.requests, "get", lambda *a, **kw: pytest.fail("cached video should upgrade locally"))
    result = webfetch.acquire({"url": url}, tmp_path / "download", cache)
    assert result["status"] == "verified"
    assert (result["width"], result["height"]) == (720, 1280)
    with Image.open(result["thumbnail"]) as image:
        assert image.size == (360, 640)
        assert_direction(image, 90)


def test_pixel_aspect_stretch_does_not_pass_low_resolution_video(tmp_path):
    source = make_video(tmp_path / "media", size="320x480", sar="2/1")
    with pytest.raises(ValueError, match="480"):
        webfetch.inspect_media(source)


def test_legacy_video_dimensions_do_not_claim_verified_display_orientation():
    state = {"media": [{"name": "old.mp4", "kind": "video", "width": 1920, "height": 1080}],
             "timeline": [{"media": "old.mp4", "kind": "video", "use_duration": 1}]}
    check = next(c for c in build_delivery(state, "bilibili")["checks"] if c["code"] == "source_geometry")
    assert check["status"] == "review" and "按旧尺寸估算" in check["message"]
