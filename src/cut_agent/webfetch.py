"""Bounded download, decode validation and cross-run URL cache for web media."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

from PIL import Image, ImageOps
import requests

from .media import (_ffmpeg, _ffprobe, _parse_fps, display_geometry, video_fit_filter,
                    DISPLAY_GEOMETRY_VERSION, image_display_geometry)
from .runctl import read_json, write_json_atomic, retry_checkpoint, RunHalted
from .source_timing import FrameIndex, probe_timing

MAX_BYTES = 80 * 1024 * 1024


def inspect_media(path: Path) -> dict:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            geometry = image_display_geometry(image)
            width, height = geometry["width"], geometry["height"]
            fmt = image.format.lower()
        return {"kind": "image", "width": width, "height": height, "display_geometry": geometry,
                "duration": 0, "fps": 0, "extension": ".jpg" if fmt == "jpeg" else "." + fmt}
    except (OSError, ValueError):
        pass
    probe = subprocess.run([_ffprobe(), "-v", "error", "-show_streams", "-show_format",
                            "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    if probe.returncode:
        raise ValueError("响应不是可识别的图片或视频（可能为网页/验证页）")
    data = json.loads(probe.stdout)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    geometry = display_geometry(stream)
    width, height = geometry["width"], geometry["height"]
    duration = float(data.get("format", {}).get("duration") or stream.get("duration") or 0)
    if min(width, height, geometry["encoded_width"], geometry["encoded_height"]) < 480 or duration <= 0:
        raise ValueError("视频短边不足 480 像素或时长无效")
    decode = subprocess.run([_ffmpeg(), "-v", "error", "-xerror", "-i", str(path),
                             "-map", "0:v:0", "-f", "null", "-"], capture_output=True, timeout=60)
    if decode.returncode:
        raise ValueError("视频解码失败")
    timing = probe_timing(path, stream, duration)
    index = FrameIndex.optional(timing)
    if index:
        duration = index.seconds(index.count)
    format_name = data.get("format", {}).get("format_name", "")
    return {"kind": "video", "width": width, "height": height, "duration": duration,
            "fps": _parse_fps(stream.get("avg_frame_rate", "")),
            "source_timing": timing,
            "display_geometry": geometry,
            "extension": ".webm" if "webm" in format_name else ".mp4"}


def acquire(asset: dict, destination: Path, cache: Path, *, max_bytes: int = MAX_BYTES) -> dict:
    result = {**asset, "status": "manual"}
    url = str(asset.get("url") or "")
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError("缺少可下载的 HTTP(S) 素材链接")
        key = hashlib.sha256(url.encode()).hexdigest()
        entry = cache / key
        entry.mkdir(parents=True, exist_ok=True)
        metadata = read_json(entry / "media.json")
        original = entry / ("media" + metadata["extension"]) if isinstance(metadata, dict) else None
        if (original is None or not original.is_file() or original.stat().st_size > max_bytes
                or hashlib.sha256(original.read_bytes()).hexdigest() != metadata.get("sha256")):
            with tempfile.TemporaryDirectory(prefix=".fetch-", dir=entry) as temporary:
                temporary = Path(temporary).resolve()
                assert temporary.is_relative_to(entry.resolve())
                download = temporary / "download"
                # Small Range GET is a liveness probe; some CDNs reject HEAD.
                retry_checkpoint()
                with requests.get(url, headers={"Range": "bytes=0-1023"}, stream=True, timeout=(10, 30)) as probe:
                    probe.raise_for_status()
                    if probe.status_code not in (200, 206):
                        raise ValueError("服务器没有返回媒体内容")
                    total = probe.headers.get("Content-Range", "").split("/")[-1]
                    if total.isdigit() and int(total) > max_bytes:
                        raise ValueError("素材超过下载大小上限")
                retry_checkpoint()
                with requests.get(url, stream=True, timeout=(10, 30)) as response:
                    response.raise_for_status()
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit() and int(length) > max_bytes:
                        raise ValueError("素材超过下载大小上限")
                    size = 0
                    with download.open("wb") as target:
                        for chunk in response.iter_content(64 * 1024):
                            retry_checkpoint()
                            size += len(chunk)
                            if size > max_bytes:
                                raise ValueError("素材超过下载大小上限")
                            target.write(chunk)
                metadata = inspect_media(download)
                metadata.update(url=url, size=download.stat().st_size,
                                sha256=hashlib.sha256(download.read_bytes()).hexdigest())
                original = entry / ("media" + metadata["extension"])
                download.replace(original)
                write_json_atomic(entry / "media.json", metadata)
        if ((metadata.get("display_geometry") or {}).get("version") != DISPLAY_GEOMETRY_VERSION
                or (metadata["kind"] == "video" and FrameIndex.optional(metadata.get("source_timing")) is None)):
            # Upgrade older cached downloads locally, without fetching the URL again.
            metadata.update(inspect_media(original))
            write_json_atomic(entry / "media.json", metadata)
        # Check that the cached payload still matches the verified content.
        if hashlib.sha256(original.read_bytes()).hexdigest() != metadata["sha256"]:
            raise ValueError("缓存素材内容已变化，需重新下载")
        destination.mkdir(parents=True, exist_ok=True)
        local = destination / (key[:20] + metadata["extension"])
        shutil.copy2(original, local)
        result.update(metadata, status="verified", local=str(local.resolve()), error=None)
        if metadata["kind"] == "video":
            thumb = destination / (key[:20] + ".jpg")
            subprocess.run([_ffmpeg(), "-v", "error", "-y", "-i", str(local),
                            "-frames:v", "1", "-vf", video_fit_filter(640, 640), str(thumb)],
                           capture_output=True, check=True, timeout=30)
            result["thumbnail"] = str(thumb.resolve())
        else:
            thumb = destination / (key[:20] + ".thumbnail.png")
            with Image.open(local) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((640, 640))
                image.save(thumb)
            result["thumbnail"] = str(thumb.resolve())
    except RunHalted:
        raise
    except Exception as exc:
        result.update(status="manual", error=str(exc)[:300])
        result.pop("local", None)
        result.pop("thumbnail", None)
    return result
