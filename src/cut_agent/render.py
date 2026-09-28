"""渲染层：按粗剪时间线用 ffmpeg 切段 / 拼接 / 叠 BGM，产出粗剪成片视频。

粗剪成片 = 第一遍结构搭建的可视化：
- 每段按时间线的 start_offset / use_duration 切出，图片按展示时长定格；
- 预览片段归一到 1280x720 / yuv420p / h264，使用执行卡 fps（默认 25）后拼接；
- 预览 BGM 音量默认 0.4，音乐比片长短则补静音；
- 分段与最终视频均解码核对帧数、帧率与时长，短于方案时失败，不压缩时间轴；
- 不做转场 / 调色 / 字幕（那是精剪阶段的事）。

任何一步失败都抛出 RenderError，由上层节点降级（不阻塞文档产出）。
"""
from __future__ import annotations

import subprocess
import json
import math
import re
from fractions import Fraction
from pathlib import Path

from .media import _ffmpeg, _ffprobe, video_fit_filter
from .runctl import retry_checkpoint, read_json, write_json_atomic
from .checkpoints import fingerprint
from .storyboard import frame_index
from .source_timing import FrameIndex, align_timeline

TARGET_W, TARGET_H = 1280, 720
FPS = 30
BGM_VOLUME = 0.18

# 归一化滤镜：等比缩放 + 黑边补齐到 1280x720，统一 SAR 与帧率
_NORM_VF = (
    video_fit_filter(TARGET_W, TARGET_H) + ","
    f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
    f"setsar=1,fps={FPS}"
)


class RenderError(RuntimeError):
    pass


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RenderError(f"ffmpeg 失败（rc={r.returncode}）: {r.stderr[-400:]}")
    return r


def _duration_of(p: Path) -> float:
    r = subprocess.run(
        [_ffprobe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(p)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _check_video(p: Path, frames: int, fps: float, context: str) -> None:
    """Decode and verify preview geometry/timing before checkpointing or publishing."""
    try:
        result = subprocess.run(
            [_ffprobe(), "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=nb_read_frames,duration,start_time,avg_frame_rate,width,height",
             "-of", "json", str(p)], capture_output=True, text=True,
            timeout=max(60, frames / fps * 5),
        )
        if result.returncode or result.stderr.strip():
            raise ValueError("视频解码失败：" + result.stderr[-200:])
        stream = json.loads(result.stdout)["streams"][0]
        actual = int(stream["nb_read_frames"])
        if actual != frames:
            raise ValueError(f"需要 {frames} 帧，实际解码 {actual} 帧；请检查源素材可用画面和入/出点")
        rate = float(Fraction(stream["avg_frame_rate"]))
        duration, start = float(stream["duration"]), float(stream["start_time"])
        if (not math.isclose(rate, fps, rel_tol=1e-6)
                or not math.isclose(duration, frames / fps, abs_tol=.001, rel_tol=0)
                or not math.isclose(start, 0, abs_tol=.001)
                or (stream["width"], stream["height"]) != (TARGET_W, TARGET_H)):
            raise ValueError("编码画幅、帧率或时长与执行卡不一致")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError,
            ZeroDivisionError) as exc:
        raise RenderError(f"{context}校验失败：{exc}") from exc


def _filter_value(value: str) -> str:
    # ffmpeg parses option values, then filtergraph syntax; subprocess adds no shell layer.
    value = "".join("\\" + c if c in "\\':" else c for c in value)
    return "".join("\\" + c if c in "\\'[],;" else c for c in value)


def _still_input(src: Path, out_path: Path) -> Path:
    """Use the storyboard's first image for formats without image2 looping."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with Image.open(src) as image:
            if image.format != "GIF" and not getattr(image, "is_animated", False):
                return src
            image.seek(0)
            still = out_path.with_suffix(".still.png")
            ImageOps.exif_transpose(image).convert("RGB").save(still)
            return still
    except UnidentifiedImageError:
        # Preserve the existing ffmpeg path for formats not supported by Pillow.
        return src
    except (OSError, ValueError) as exc:
        raise RenderError(f"图片首帧读取失败（{src.name}）：{exc}") from exc


def normalize_segment(src: Path, kind: str, start_offset: float, use_duration: float,
                      out_path: Path, *, fps: float = FPS, label: str | None = None,
                      source_frames: list[int] | None = None,
                      source_timing: dict | None = None,
                      legacy_full_decode: bool = False) -> None:
    """把一段素材（视频片段或定格图片）编码为统一规格的 mp4。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames = frame_index(use_duration, fps)
    base = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    vf = _NORM_VF.replace(f"fps={FPS}", f"fps={fps}")
    if label is not None:
        label_file = out_path.with_suffix(".label.txt")
        label_file.write_text(label, encoding="utf-8")
        font = Path("C:/Windows/Fonts/msyh.ttc")
        font_arg = f"fontfile={_filter_value(font.resolve().as_posix())}:" if font.is_file() else ""
        vf += f",drawtext={font_arg}textfile={_filter_value(label_file.resolve().as_posix())}:expansion=none:fontsize=24:fontcolor=white:box=1:boxcolor=black@0.7:x=16:y=16"
    if kind == "video":
        if source_frames is not None:
            first, end = source_frames
            indexed_vf = vf.replace(f"fps={fps}", f"fps={fps}:round=up:eof_action=pass", 1)
            index = FrameIndex.optional(source_timing)
            if index is not None and 0 < first < end <= index.count:
                # Seek between native frames, then verify the decoded first frame's
                # original PTS. Some demuxers seek imprecisely; those fall back below.
                seek = (index.ticks[first - 1] + index.ticks[first]) * index.base / 2
                expected_pts = index.origin + index.ticks[first]
                fast_vf = (f"trim=start_pts={expected_pts},trim=end_frame={end - first},"
                           f"showinfo,setpts=PTS-STARTPTS,{indexed_vf}")
                fast_cmd = ([_ffmpeg(), "-hide_banner", "-loglevel", "info", "-nostats", "-y",
                             "-copyts", "-ss", f"{seek:.12f}", "-i", str(src),
                             "-frames:v", str(frames), "-an", "-vf", fast_vf,
                             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                             "-pix_fmt", "yuv420p", str(out_path)])
                try:
                    result = _run(fast_cmd, timeout=max(120.0, use_duration * 15))
                    first_pts = re.search(r"showinfo[^\n]*\bn:\s*0\s+pts:\s*(-?\d+)", result.stderr)
                    if first_pts and int(first_pts.group(1)) == expected_pts:
                        return
                except RenderError:
                    pass
                out_path.unlink(missing_ok=True)
            vf = (f"trim=start_frame={first}:end_frame={end},setpts=PTS-STARTPTS,"
                  + indexed_vf)
            inputs = ["-i", str(src)]
        elif legacy_full_decode:
            # Old plans may lack native frame metadata. Decode from the first
            # video frame so fractional seeks do not silently lose lead frames.
            vf = (f"setpts=PTS-STARTPTS,trim=start={start_offset:.9f}:"
                  f"end={start_offset + use_duration:.9f},setpts=PTS-STARTPTS,"
                  + vf.replace(f"fps={fps}", f"fps={fps}:round=up:eof_action=pass", 1))
            inputs = ["-i", str(src)]
        else:
            inputs = ["-ss", f"{max(0.0, start_offset):.9f}", "-i", str(src), "-t", f"{use_duration:.9f}"]
        cmd = (base + inputs + ["-frames:v", str(frames), "-an",
                       "-vf", vf,
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                       "-pix_fmt", "yuv420p", str(out_path)])
    else:
        src = _still_input(src, out_path)
        cmd = (base + ["-loop", "1", "-framerate", str(fps),
                       "-i", str(src), "-frames:v", str(frames),
                       "-vf", vf,
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                       "-pix_fmt", "yuv420p", str(out_path)])
    _run(cmd, timeout=max(120.0, use_duration * 15,
                          (start_offset + use_duration) * 5 if source_frames or legacy_full_decode else 0))


def _concat_copy(seg_paths: list[Path], out_path: Path) -> bool:
    """concat demuxer + 流拷贝；失败返回 False（由上层换 filter 重编码）。"""
    list_file = out_path.parent / "concat_list.txt"
    def quoted(path):
        return "'" + path.resolve().as_posix().replace("'", "'\\''") + "'"
    list_file.write_text(
        "\n".join(f"file {quoted(p)}" for p in seg_paths) + "\n", encoding="utf-8")
    try:
        _run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
              "-f", "concat", "-safe", "0", "-i", str(list_file),
              "-c", "copy", str(out_path)], timeout=600)
        return True
    except RenderError:
        return False


def _concat_reencode(seg_paths: list[Path], out_path: Path) -> None:
    """回退：concat filter 重编码（对编码参数不一致更宽容）。"""
    args = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    for p in seg_paths:
        args += ["-i", str(p)]
    n = len(seg_paths)
    fc = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    args += ["-filter_complex", fc, "-map", "[v]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", str(out_path)]
    _run(args, timeout=max(300.0, n * 60))


def _mux_bgm(video_path: Path, music_path: Path, out_path: Path, total_dur: float,
             volume: float = BGM_VOLUME) -> bool:
    """叠 BGM：视频流拷贝，音频 = BGM 压音量 + 补静音到片长。失败返回 False。"""
    if not music_path.exists() or music_path.stat().st_size < 10_000:
        return False
    try:
        _run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
              "-i", str(video_path), "-i", str(music_path),
              "-filter_complex",
              f"[1:a]volume={volume},apad,atrim=0:{total_dur:.3f}[a]",
              "-map", "0:v:0", "-map", "[a]",
              "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out_path)],
             timeout=300)
        return True
    except RenderError:
        return False


def render_video(timeline: list[dict], media_folder: Path,
                 media_index: dict[str, dict],
                 music_path: Path | None, out_path: Path, *,
                 fps: float = 25, volume: float = .4, progress=None) -> dict:
    """Opt-in preview with labels and per-segment resume; missing rows retain their duration."""
    from PIL import Image
    if not math.isfinite(fps) or fps <= 0:
        raise RenderError("预览 fps 必须为正数")
    timeline = align_timeline(timeline, list(media_index.values()))
    workdir = out_path.parent / "preview_work"
    workdir.mkdir(parents=True, exist_ok=True)
    paths, skipped, markers = [], [], []
    elapsed = 0.0
    for i, row in enumerate(timeline, 1):
        retry_checkpoint()
        name = row.get("media") or "missing"
        duration = float(row.get("use_duration", 6))
        if not math.isfinite(duration) or duration <= 0:
            raise RenderError(f"第 {i} 行时长必须为正数")
        marker_start = frame_index(elapsed, fps) / fps
        marker_end = frame_index(elapsed + duration, fps) / fps
        encoded_duration = marker_end - marker_start
        if encoded_duration <= 0:
            raise RenderError(f"第 {i} 行不足一帧，无法预览")
        offset = float(row.get("start_offset", 0))
        if not math.isfinite(offset) or offset < 0:
            raise RenderError(f"第 {i} 行入点必须为非负数")
        is_web = row.get("source") == "web"
        src = (Path(row["local_path"]) if is_web and row.get("asset_status") == "verified" and row.get("local_path")
               else media_folder / name)
        kind = row.get("kind", "video")
        missing = not src.is_file() or (is_web and row.get("asset_status") != "verified")
        if missing:
            skipped.append(f"#{i} {name} 素材待补，保留等时长占位")
            src = workdir / f"missing_{i:03d}.png"
            if not src.is_file():
                Image.new("RGB", (TARGET_W, TARGET_H), "#182130").save(src)
            kind, offset = "image", 0
        source_frames = None
        if kind == "video":
            metadata = row if is_web else media_index.get(name, {})
            index = FrameIndex.optional(metadata.get("source_timing"))
            if index:
                if offset + duration > index.seconds(index.count) + 1e-6:
                    raise RenderError(f"第 {i} 行入/出点超过可解码源帧范围")
                source_frames = [index.nearest(offset, allow_end=False), index.nearest(offset + duration)]
        label = f"#{i} {marker_start:.2f}-{marker_end:.2f}s {name}" + (" [MISSING]" if missing else "")
        key = {"source": str(src.resolve()), "fingerprint": fingerprint(src), "kind": kind,
               "offset": offset, "duration": encoded_duration, "label": label, "fps": fps,
               "version": 5 if kind == "image" else 6}
        if source_frames is not None:
            key["source_frames"] = source_frames
        segment = workdir / f"seg_{i:03d}.mp4"
        checkpoint = segment.with_suffix(".json")
        expected_frames = frame_index(encoded_duration, fps)
        reusable = read_json(checkpoint) == key and segment.is_file()
        if reusable:
            try:
                _check_video(segment, expected_frames, fps, f"第 {i} 段缓存")
            except RenderError:
                reusable = False
        if not reusable:
            temporary = segment.with_suffix(".partial.mp4")
            normalize_segment(src, kind, offset, encoded_duration, temporary, fps=fps, label=label,
                              source_frames=source_frames,
                              source_timing=metadata.get("source_timing") if kind == "video" else None)
            try:
                _check_video(temporary, expected_frames, fps, f"第 {i} 段（{name}）")
            except RenderError:
                if kind != "video" or source_frames is not None:
                    raise
                temporary.unlink(missing_ok=True)
                normalize_segment(src, kind, offset, encoded_duration, temporary, fps=fps, label=label,
                                  legacy_full_decode=True)
                _check_video(temporary, expected_frames, fps, f"第 {i} 段（{name}）")
            temporary.replace(segment)
            write_json_atomic(checkpoint, key)
        paths.append(segment)
        markers.append({"seq": i, "start": marker_start, "end": marker_end,
                        "media": name, "missing": missing})
        elapsed += duration
        if progress:
            progress(i, len(timeline), name)
    retry_checkpoint()
    if not paths:
        raise RenderError("时间线为空，无法预览")
    silent = workdir / "concat.mp4"
    if not _concat_copy(paths, silent):
        _concat_reencode(paths, silent)
    retry_checkpoint()
    total_frames = frame_index(elapsed, fps)
    duration = total_frames / fps
    final = workdir / "final.mp4"
    has_music = music_path is not None and _mux_bgm(silent, music_path, final, duration, volume)
    if not has_music:
        silent.replace(final)
    _check_video(final, total_frames, fps, "最终预览")
    final.replace(out_path)
    return {"n_segments": len(paths), "skipped": skipped, "has_music": has_music,
            "duration": round(duration, 3), "size_mb": round(out_path.stat().st_size / 1024 / 1024, 2),
            "markers": markers, "fps": fps, "frame_count": total_frames,
            "bgm_volume": volume, "path": str(out_path.resolve())}
