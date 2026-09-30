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
SOURCE_VOLUME = 1.0
NARRATION_VOLUME = 1.0
DUCKING_DB = -12.0

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


def _has_audio(p: Path) -> bool:
    """Return whether ffmpeg can see an audio stream (probe failures are silence)."""
    result = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, timeout=60,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _check_video(p: Path, frames: int, fps: float, context: str, *,
                 width: int = TARGET_W, height: int = TARGET_H) -> None:
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
                or (stream["width"], stream["height"]) != (width, height)):
            raise ValueError("编码画幅、帧率或时长与执行卡不一致")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError,
            ZeroDivisionError) as exc:
        raise RenderError(f"{context}校验失败：{exc}") from exc


def _check_audio(p: Path, duration: float, fps: float, context: str) -> None:
    result = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", str(p)],
        capture_output=True, text=True, timeout=max(60, duration * 5),
    )
    try:
        actual = float(result.stdout.strip())
        if result.returncode or not math.isclose(actual, duration, abs_tol=1 / fps + .02):
            raise ValueError(f"需要 {duration:.3f}s 音频，实际 {actual:.3f}s")
    except (ValueError, TypeError) as exc:
        raise RenderError(f"{context}音轨校验失败：{exc}") from exc


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
                      width: int = TARGET_W, height: int = TARGET_H,
                      source_frames: list[int] | None = None,
                      source_timing: dict | None = None,
                      legacy_full_decode: bool = False) -> None:
    """把一段素材（视频片段或定格图片）编码为统一规格的 mp4。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames = frame_index(use_duration, fps)
    base = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    vf = (video_fit_filter(width, height) + ","
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}")
    if label is not None:
        label_file = out_path.with_suffix(".label.txt")
        label_file.write_text(label, encoding="utf-8")
        font = Path("C:/Windows/Fonts/msyh.ttc")
        font_arg = f"fontfile={_filter_value(font.resolve().as_posix())}:" if font.is_file() else ""
        vf += f",drawtext={font_arg}textfile={_filter_value(label_file.resolve().as_posix())}:expansion=none:fontsize=24:fontcolor=white:box=1:boxcolor=black@0.7:x=16:y=16"
    source_has_audio = kind == "video" and _has_audio(src)
    if kind == "video":
        if source_frames is not None:
            first, end = source_frames
            indexed_vf = vf.replace(f"fps={fps}", f"fps={fps}:round=up:eof_action=pass", 1)
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
        audio_input = [] if source_has_audio else ["-f", "lavfi", "-t", f"{use_duration:.9f}",
                                                    "-i", "anullsrc=r=48000:cl=stereo"]
        audio_index = 0 if source_has_audio else 1
        audio_start = start_offset if source_has_audio and (source_frames is not None or legacy_full_decode) else 0
        af = (f"[{audio_index}:a:0]atrim=start={audio_start:.9f}:end={audio_start + use_duration:.9f},"
              "asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
              f"apad,atrim=0:{use_duration:.9f}[a]")
        cmd = (base + inputs + audio_input + ["-frames:v", str(frames),
                       "-filter_complex", f"[0:v]{vf}[v];{af}", "-map", "[v]", "-map", "[a]",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                       "-shortest", str(out_path)])
    else:
        src = _still_input(src, out_path)
        cmd = (base + ["-loop", "1", "-framerate", str(fps), "-i", str(src),
                       "-f", "lavfi", "-t", f"{use_duration:.9f}", "-i", "anullsrc=r=48000:cl=stereo",
                       "-frames:v", str(frames), "-map", "0:v:0", "-map", "1:a:0", "-vf", vf,
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out_path)])
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


def _add_silent_audio(video_path: Path, duration: float) -> None:
    """Give externally rendered video the same stereo audio layout as every segment."""
    output = video_path.with_name(video_path.stem + ".audio" + video_path.suffix)
    _run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path),
          "-f", "lavfi", "-t", f"{duration:.9f}", "-i", "anullsrc=r=48000:cl=stereo",
          "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
          "-shortest", str(output)], timeout=max(120.0, duration * 5))
    output.replace(video_path)


def _concat_reencode(seg_paths: list[Path], out_path: Path) -> None:
    """回退：concat filter 重编码（对编码参数不一致更宽容）。"""
    args = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    for p in seg_paths:
        args += ["-i", str(p)]
    n = len(seg_paths)
    fc = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    args += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", "-c:a", "aac", str(out_path)]
    _run(args, timeout=max(300.0, n * 60))


def _mux_bgm(video_path: Path, music_path: Path | None, out_path: Path, total_dur: float,
             volume: float = BGM_VOLUME, *, narration_path: Path | None = None,
             narration_segments: list[dict] | None = None, source_volume: float = SOURCE_VOLUME,
             narration_volume: float = NARRATION_VOLUME, ducking_db: float = DUCKING_DB) -> dict:
    """Mix source sound, timeline-positioned narration and looped/ducked BGM."""
    has_music = bool(music_path and music_path.is_file() and music_path.stat().st_size >= 10_000)
    has_narration = bool(narration_path and narration_path.is_file() and _has_audio(narration_path))
    if not has_music and not has_narration:
        return {"ok": False, "has_music": False, "has_narration": False}
    try:
        args = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path)]
        filters = [f"[0:a]volume={source_volume},atrim=0:{total_dur:.6f}[source]"]
        voices = []
        next_input = 1
        if has_narration:
            args += ["-i", str(narration_path)]
            segments = narration_segments or [{"source_start": 0, "source_end": total_dur, "timeline_start": 0}]
            for j, seg in enumerate(segments):
                start, end, at = seg["source_start"], seg["source_end"], seg["timeline_start"]
                delay = max(0, round(float(at) * 1000))
                filters.append(f"[{next_input}:a]atrim={start}:{end},asetpts=PTS-STARTPTS,volume={narration_volume},adelay={delay}|{delay}[voice{j}]")
                voices.append(f"[voice{j}]")
            next_input += 1
        if has_music:
            args += ["-stream_loop", "-1", "-i", str(music_path)]
            filters.append(f"[{next_input}:a]volume={volume},atrim=0:{total_dur:.6f}[music]")
        mix_inputs = ["[source]"] + voices
        if voices:
            voice_tail = ",asplit=2[voice_mix][voice_sc]" if has_music else "[voice_mix]"
            filters.append("".join(voices) + f"amix=inputs={len(voices)}:normalize=0,atrim=0:{total_dur:.6f}" + voice_tail)
            mix_inputs = ["[source]", "[voice_mix]"]
        if has_music and voices:
            ratio = max(1.0, min(20.0, abs(ducking_db) / 2))
            filters.append(f"[music][voice_sc]sidechaincompress=threshold=0.02:ratio={ratio}:attack=20:release=300[ducked]")
            mix_inputs.append("[ducked]")
        elif has_music:
            mix_inputs.append("[music]")
        filters.append("".join(mix_inputs) + f"amix=inputs={len(mix_inputs)}:normalize=0,apad,atrim=0:{total_dur:.6f}[a]")
        args += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[a]",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{total_dur:.6f}", str(out_path)]
        _run(args, timeout=300)
        return {"ok": True, "has_music": has_music, "has_narration": has_narration}
    except RenderError:
        return {"ok": False, "has_music": False, "has_narration": False}


def render_video(timeline: list[dict], media_folder: Path,
                 media_index: dict[str, dict],
                 music_path: Path | None, out_path: Path, *,
                 fps: float = 25, volume: float = .4, progress=None,
                 narration_path: Path | None = None,
                 narration_segments: list[dict] | None = None,
                 source_volume: float = SOURCE_VOLUME,
                 narration_volume: float = NARRATION_VOLUME,
                 ducking_db: float = DUCKING_DB) -> dict:
    """Opt-in preview with labels and per-segment resume; missing rows retain their duration."""
    from PIL import Image
    if not math.isfinite(fps) or fps <= 0:
        raise RenderError("预览 fps 必须为正数")
    timeline = align_timeline(timeline, list(media_index.values()))
    html_sizes = [(int(row.get("width", 0)), int(row.get("height", 0))) for row in timeline
                  if row.get("source") == "generated" and row.get("kind") == "html"]
    target_w, target_h = max(html_sizes, key=lambda size: size[0] * size[1]) if html_sizes else (TARGET_W, TARGET_H)
    if (target_w, target_h) not in ((1920, 1080), (3840, 2160), (TARGET_W, TARGET_H)):
        raise RenderError("HTML 动画分辨率无效")
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
        is_html = row.get("source") == "generated" and row.get("kind") == "html"
        html_pages = row.get("html_pages") or ([{"path": row.get("local_path"), "duration": duration}] if is_html else [])
        src = (Path(row["local_path"]) if is_html and row.get("local_path")
               else Path(row["local_path"]) if is_web and row.get("asset_status") == "verified" and row.get("local_path")
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
        page_fingerprints = ([{"path": str(Path(page["path"]).resolve()),
                               "fingerprint": fingerprint(Path(page["path"])), "duration": page["duration"]}
                              for page in html_pages] if is_html else None)
        source_has_audio = kind == "video" and _has_audio(src)
        key = {"source": str(src.resolve()), "fingerprint": fingerprint(src), "kind": kind,
               "offset": offset, "duration": encoded_duration, "label": label, "fps": fps,
               "width": target_w, "height": target_h,
               "source_audio": source_has_audio,
               "version": 2 if is_html else 6 if kind == "image" else 7}
        if page_fingerprints is not None:
            key["html_pages"] = page_fingerprints
        if source_frames is not None:
            key["source_frames"] = source_frames
        segment = workdir / f"seg_{i:03d}.mp4"
        checkpoint = segment.with_suffix(".json")
        expected_frames = frame_index(encoded_duration, fps)
        reusable = read_json(checkpoint) == key and segment.is_file()
        if reusable:
            try:
                _check_video(segment, expected_frames, fps, f"第 {i} 段缓存", width=target_w, height=target_h)
                _check_audio(segment, encoded_duration, fps, f"第 {i} 段缓存")
            except RenderError:
                reusable = False
        if not reusable:
            temporary = segment.with_suffix(".partial.mp4")
            if is_html:
                from .html_motion import render_pages
                render_pages(html_pages, temporary, encoded_duration, fps, width=target_w, height=target_h)
                _add_silent_audio(temporary, encoded_duration)
            else:
                normalize_segment(src, kind, offset, encoded_duration, temporary, fps=fps, label=label,
                                  width=target_w, height=target_h,
                                  source_frames=source_frames,
                                  source_timing=metadata.get("source_timing") if kind == "video" else None)
            try:
                _check_video(temporary, expected_frames, fps, f"第 {i} 段（{name}）",
                             width=target_w, height=target_h)
                _check_audio(temporary, encoded_duration, fps, f"第 {i} 段（{name}）")
            except RenderError:
                if kind != "video" or source_frames is not None:
                    raise
                temporary.unlink(missing_ok=True)
                normalize_segment(src, kind, offset, encoded_duration, temporary, fps=fps, label=label,
                                  width=target_w, height=target_h, legacy_full_decode=True)
                _check_video(temporary, expected_frames, fps, f"第 {i} 段（{name}）",
                             width=target_w, height=target_h)
                _check_audio(temporary, encoded_duration, fps, f"第 {i} 段（{name}）")
            temporary.replace(segment)
            write_json_atomic(checkpoint, key)
        paths.append(segment)
        markers.append({"seq": i, "start": marker_start, "end": marker_end,
                        "media": name, "missing": missing,
                        "source_audio": source_has_audio})
        if kind == "video" and not source_has_audio:
            skipped.append(f"#{i} {name} 无源音轨，已补等时长静音")
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
    mix_fingerprint = {
        "version": 2, "video": fingerprint(silent),
        "music": fingerprint(music_path) if music_path else None,
        "narration": fingerprint(narration_path) if narration_path else None,
        "narration_segments": narration_segments or [], "bgm_volume": volume,
        "source_volume": source_volume, "narration_volume": narration_volume,
        "ducking_db": ducking_db, "duration": duration,
    }
    mixed = _mux_bgm(silent, music_path, final, duration, volume,
                     narration_path=narration_path, narration_segments=narration_segments,
                     source_volume=source_volume, narration_volume=narration_volume,
                     ducking_db=ducking_db)
    if not mixed["ok"]:
        silent.replace(final)
    write_json_atomic(workdir / "mix.json", mix_fingerprint)
    _check_video(final, total_frames, fps, "最终预览", width=target_w, height=target_h)
    _check_audio(final, duration, fps, "最终预览")
    final.replace(out_path)
    tracks = [{"type": "source", "gain": source_volume,
               "segments_with_audio": sum(bool(m["source_audio"]) for m in markers)}]
    if mixed["has_narration"]:
        tracks.append({"type": "narration", "path": str(narration_path.resolve()),
                       "gain": narration_volume, "segments": len(narration_segments or [])})
    if mixed["has_music"]:
        tracks.append({"type": "bgm", "path": str(music_path.resolve()), "gain": volume,
                       "ducking_db": ducking_db, "looped": True})
    return {"n_segments": len(paths), "skipped": skipped, "has_music": mixed["has_music"],
            "has_narration": mixed["has_narration"], "audio_tracks": tracks,
            "audio_warnings": [warning for warning in skipped if "音轨" in warning],
            "duration": round(duration, 3), "size_mb": round(out_path.stat().st_size / 1024 / 1024, 2),
            "markers": markers, "fps": fps, "frame_count": total_frames,
            "bgm_volume": volume, "source_volume": source_volume,
            "narration_volume": narration_volume, "ducking_db": ducking_db,
            "mix_fingerprint": mix_fingerprint, "width": target_w, "height": target_h,
            "path": str(out_path.resolve())}
