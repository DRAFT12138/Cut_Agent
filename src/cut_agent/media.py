"""媒体探测层：用 ffprobe/ffmpeg 读取时长、场景切点、抽帧缩略图。"""
from __future__ import annotations

import json
import hashlib
import math
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from PIL import Image

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".ts", ".flv", ".wmv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}
DISPLAY_GEOMETRY_VERSION = 1


class MediaError(RuntimeError):
    pass


def display_geometry(stream: dict) -> dict:
    """Square-pixel display dimensions, matching ffmpeg's automatic rotation."""
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    try:
        sar = Fraction(str(stream.get("sample_aspect_ratio", "1:1")).replace(":", "/"))
        if sar <= 0:
            sar = Fraction(1)
    except (ValueError, ZeroDivisionError):
        sar = Fraction(1)
    rotation = next((side["rotation"] for side in stream.get("side_data_list", [])
                     if "rotation" in side), stream.get("tags", {}).get("rotate", 0))
    try:
        rotation = float(rotation)
        if not math.isfinite(rotation):
            rotation = 0.0
    except (TypeError, ValueError):
        rotation = 0.0
    dw, dh = round(width * sar), height
    angle = rotation % 360
    if abs(angle - 90) < 1 or abs(angle - 270) < 1:
        dw, dh = dh, dw
    return {"version": DISPLAY_GEOMETRY_VERSION, "encoded_width": width, "encoded_height": height,
            "sample_aspect_ratio": str(sar), "rotation": rotation, "width": dw, "height": dh}


def video_fit_filter(width: int, height: int) -> str:
    # Normalize pixel aspect before fitting. This works with FFmpeg versions
    # whose scale filter does not yet support reset_sar.
    return (f"scale=iw*sar:ih,setsar=1,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2")


def image_display_geometry(image: Image.Image) -> dict:
    orientation = image.getexif().get(274, 1)
    if orientation not in range(1, 9):
        orientation = 1
    width, height = image.size
    return {"version": DISPLAY_GEOMETRY_VERSION, "encoded_width": width, "encoded_height": height,
            "exif_orientation": orientation,
            "width": height if orientation >= 5 else width,
            "height": width if orientation >= 5 else height}


def _local_tool(name: str) -> str | None:
    """优先用项目内置 tools/ffmpeg-*/bin（免安装、版本漂移无关）。"""
    from .config import ROOT
    tools = ROOT / "tools"
    if tools.is_dir():
        for d in sorted(tools.iterdir()):
            cand = d / "bin" / f"{name}.exe" if d.is_dir() else None
            if cand is None:
                cand = d / "bin" / name
            if cand.exists():
                return str(cand)
    return None


def _ffmpeg() -> str:
    p = _local_tool("ffmpeg") or shutil.which("ffmpeg")
    if not p:
        raise MediaError("未找到 ffmpeg：请运行 python make_sample.py 或手动下载 ffmpeg 到 tools/ 或 PATH")
    return p


def _ffprobe() -> str:
    p = _local_tool("ffprobe") or shutil.which("ffprobe")
    if not p:
        raise MediaError("未找到 ffprobe：请运行 python make_sample.py 或手动下载 ffmpeg 到 tools/ 或 PATH")
    return p


@dataclass
class MediaItem:
    name: str                # 文件名（相对素材目录）
    path: Path
    kind: str                # video | image
    duration: float          # 秒（图片为 0.0，展示时长由上层决定）
    width: int = 0
    height: int = 0
    fps: float = 0.0
    size_mb: float = 0.0
    scene_cuts: list[float] = field(default_factory=list)  # 视频场景切换点（秒）
    thumbnails: list[str] = field(default_factory=list)    # 抽帧路径
    source_timing: dict = field(default_factory=dict)      # 解码帧索引，与首个视频帧对齐
    display_geometry: dict = field(default_factory=dict)   # 编码尺寸/SAR/旋转依据


def probe_media(folder: Path, *, cache_dir: Path | None = None) -> list[MediaItem]:
    """扫描文件夹，返回全部媒体项（视频+图片），含时长/分辨率。"""
    if not folder.is_dir():
        raise MediaError(f"素材目录不存在: {folder}")
    items: list[MediaItem] = []
    from .mediacache import ProbeCache
    from .checkpoints import fingerprint
    cache = ProbeCache(cache_dir) if cache_dir is not None else None
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        # 跳过隐藏目录与工具产物（缩略图目录等）
        rel = p.relative_to(folder)
        if any(part.startswith(".") or part == "_thumbs" for part in rel.parts):
            continue
        ext = p.suffix.lower()
        if ext not in VIDEO_EXT | IMAGE_EXT:
            continue
        key = {"version": 4 if ext in IMAGE_EXT else 3, "path": str(p.resolve()), "file": fingerprint(p)}
        saved = cache.read(key) if cache else None
        if saved:
            try:
                items.append(MediaItem(**{**saved, "path": p, "name": rel.as_posix()}))
                continue
            except (TypeError, ValueError):
                pass
        if ext in VIDEO_EXT:
            item = _probe_video(p, folder, thumbnail_dir=cache.frame_directory() if cache else None)
        elif ext in IMAGE_EXT:
            item = _probe_image(p, folder)
        items.append(item)
        if cache:
            cache.write(key, {**vars(item), "path": str(item.path)})
    return items


def _probe_video(p: Path, folder: Path, thumbnail_dir: Path | None = None) -> MediaItem:
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(p)],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise MediaError(f"ffprobe 失败 {p.name}: {out.stderr[:300]}")
    data = json.loads(out.stdout or "{}")
    fmt = data.get("format", {})
    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    dur = float(fmt.get("duration") or vstream.get("duration") or 0.0)
    from .source_timing import probe_timing, FrameIndex
    timing = probe_timing(p, vstream, dur)
    index = FrameIndex.optional(timing)
    if index:
        dur = index.seconds(index.count)
    geometry = display_geometry(vstream)
    item = MediaItem(
        name=p.relative_to(folder).as_posix(), path=p, kind="video", duration=dur,
        width=geometry["width"], height=geometry["height"],
        fps=_parse_fps(vstream.get("avg_frame_rate", "")),
        size_mb=p.stat().st_size / 1024 / 1024,
        source_timing=timing,
        display_geometry=geometry,
    )
    # 场景切点（scene detection）
    item.scene_cuts = scene_cuts(p, dur)
    # 抽 3 帧缩略图用于人工核查 / 留证
    item.thumbnails = _extract_thumbs(p, dur, folder, thumbnail_dir)
    return item


def _probe_image(p: Path, folder: Path) -> MediaItem:
    try:
        with Image.open(p) as image:
            geometry = image_display_geometry(image)
        return MediaItem(name=p.relative_to(folder).as_posix(), path=p, kind="image", duration=0.0,
                         width=geometry["width"], height=geometry["height"], display_geometry=geometry,
                         size_mb=p.stat().st_size / 1024 / 1024)
    except (OSError, ValueError, TypeError):
        pass  # Keep ffprobe metadata fallback for formats unavailable to Pillow.
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-print_format", "json",
         "-show_streams", str(p)],
        capture_output=True, text=True, timeout=60,
    )
    w = h = 0
    if out.returncode == 0:
        try:
            d = json.loads(out.stdout or "{}")
            s = next((s for s in d.get("streams", [])), {})
            w, h = int(s.get("width") or 0), int(s.get("height") or 0)
        except Exception:
            pass
    return MediaItem(name=p.relative_to(folder).as_posix(), path=p, kind="image", duration=0.0,
                     width=w, height=h, size_mb=p.stat().st_size / 1024 / 1024)


def _parse_fps(s: str) -> float:
    m = re.match(r"(\d+(?:\.\d+)?)/(\d+)", s or "")
    if m and int(m.group(2)) > 0:
        return float(m.group(1)) / float(m.group(2))
    try:
        return float(s or 0)
    except ValueError:
        return 0.0


def scene_cuts(p: Path, duration: float, threshold: float = 0.35,
               max_scenes: int = 12) -> list[float]:
    """用 ffmpeg scene 滤镜找场景切换点。失败返回 []（不阻塞主流程）。"""
    if duration <= 0:
        return []
    try:
        out = subprocess.run(
            [_ffmpeg(), "-hide_banner", "-i", str(p),
             "-vf", f"select='gt(scene\\,{threshold})',showinfo",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=max(30, duration * 2),
        )
        pts = re.findall(r"pts_time:(\d+(?:\.\d+)?)", out.stderr or "")
        cuts = [float(x) for x in pts]
        # 去重相近点
        dedup = []
        for c in cuts:
            if not dedup or c - dedup[-1] > 1.0:
                dedup.append(c)
        return dedup[:max_scenes]
    except Exception:
        return []


def _extract_thumbs(p: Path, duration: float, folder: Path, output_dir: Path | None = None) -> list[str]:
    """在 25%/50%/75% 处抽 3 帧。"""
    if duration <= 0:
        return []
    outdir = output_dir or folder / "_thumbs"
    outdir.mkdir(exist_ok=True)
    times = [duration * f for f in (0.25, 0.5, 0.75)]
    paths = []
    source_key = hashlib.sha256(p.relative_to(folder).as_posix().encode("utf-8")).hexdigest()[:12]
    for i, t in enumerate(times):
        op = outdir / f"{source_key}_t{i+1}.jpg"
        r = subprocess.run(
            [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{t:.2f}", "-i", str(p),
             "-frames:v", "1", "-vf", video_fit_filter(640, 640), "-q:v", "3", str(op)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and op.exists() and op.stat().st_size > 500:
            paths.append(str(op))
    return paths
