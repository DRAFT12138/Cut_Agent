"""Portable storyboard assets and shared frame-accurate execution anchors."""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from .craft import narration_seconds
from .shots import number, valid_shots
from .source_timing import FrameIndex, align_timeline, timestamp


def frame_index(seconds: float, fps: float) -> int:
    return max(0, math.floor(number(seconds) * fps + .5))


def timecode(frames: int, fps: float) -> str:
    """Non-drop-frame label. Exact alignment uses frame index + actual source fps."""
    nominal = max(1, round(fps))
    seconds, frame = divmod(frames, nominal)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frame:02d}"


def source_position(seconds: float, fps: float, index: FrameIndex | None, *, allow_end=True):
    frame = index.nearest(seconds, allow_end=allow_end) if index else frame_index(seconds, fps)
    moment = index.seconds(frame) if index else frame / fps
    label = timestamp(moment) if index and index.mode == "vfr" else timecode(frame, fps)
    return frame, moment, label


def source_timing_note(index: FrameIndex | None) -> str:
    if index is None:
        return "未取得逐帧索引，源帧号按 fps 估算，需复核源画面定位。"
    if index.mode == "vfr":
        return "变帧率：源位置显示实际时间戳与解码帧号（从 0 起），不按平均 fps 换算；时间从首个视频帧起算。"
    return "源帧号从 0 起，已按解码时间戳核对；时间从首个视频帧起算。"


def representative(row, media, folder: Path):
    if row.get("source") == "web" and row.get("asset_status") == "verified" and row.get("thumbnail"):
        thumbnail = Path(row["thumbnail"])
        if thumbnail.is_file():
            return thumbnail
    if media and media.get("kind") == "image":
        source = folder / media["name"]
        return source if source.is_file() else None
    start = number(row.get("start_offset"))
    end = start + number(row.get("use_duration"))
    candidates = []
    for shot in (media or {}).get("shots", []):
        for moment, path in zip(shot.get("frame_times", []), shot.get("frames", [])):
            if start <= number(moment) < end and Path(path).is_file():
                candidates.append((abs(number(moment) - (start + end) / 2), str(path)))
    return Path(min(candidates)[1]) if candidates else None


def build_storyboard(state: dict, dest: Path, timeline_fps: float = 25) -> dict:
    timeline_fps = number(timeline_fps, 25)
    if timeline_fps <= 0:
        timeline_fps = 25
    frames_dir = dest / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    media_by_name = {m["name"]: m for m in state.get("media", [])}
    indices = {name: FrameIndex.optional(m.get("source_timing")) for name, m in media_by_name.items()}
    cards, used = [], set()
    elapsed = 0.0
    narration_seq = 0
    for i, row in enumerate(align_timeline(state.get("timeline", []), state.get("media", [])), 1):
        is_web = row.get("source") == "web"
        media = None if is_web else media_by_name.get(row.get("media"))
        duration = max(0.0, number(row.get("use_duration")))
        start = max(0.0, number(row.get("start_offset")))
        source_fps = number(row.get("source_fps") if is_web else (media or {}).get("fps"))
        index = FrameIndex.optional(row.get("source_timing")) if is_web else indices.get(row.get("media"))
        if index and index.mode == "cfr":
            source_fps = index.fps
        fps_assumed = source_fps <= 0
        fps = timeline_fps if fps_assumed else source_fps
        source_in, in_seconds, in_tc = source_position(start, fps, index)
        source_out, out_seconds, out_tc = source_position(start + duration, fps, index)
        timing_note = source_timing_note(index)
        if (media or row).get("kind") == "image":
            timing_note = "静态图片无源视频帧号，源位置为占位；执行时按成片帧锚点设置展示时长。"
        elif index and start + duration > index.seconds(index.count) + .000001:
            timing_note += f" 请求区间超过可解码画面 {start + duration - index.seconds(index.count):.3f}s，请缩短或替换素材。"
        alignment = row.get("source_alignment")
        if alignment:
            timing_note += (f" 已对齐最近完整源帧：请求起点 {alignment['requested_start']:.6f}s / "
                            f"时长 {alignment['requested_duration']:.6f}s，实际起点 {start:.6f}s / "
                            f"时长 {duration:.6f}s（至少保留一帧）。")
        timeline_in, timeline_out = frame_index(elapsed, timeline_fps), frame_index(elapsed + duration, timeline_fps)
        image = representative(row, media, Path(state["media_folder"]))
        target = frames_dir / f"storyboard_{i:04d}.png"
        if image:
            # Normalize to PNG so exported references work for all supported image formats.
            try:
                with Image.open(image) as im:
                    ImageOps.exif_transpose(im).convert("RGB").save(target)
            except (OSError, ValueError):
                image = None
        if image is None:
            im = Image.new("RGB", (640, 360), "#182130")
            ImageDraw.Draw(im).text((30, 150), f"#{i} - Preview unavailable / manual review", fill="white")
            im.save(target)
        estimate = narration_seconds(str(row.get("segment_text", "")))
        has_narration = bool(str(row.get("segment_text") or "").strip())
        if has_narration:
            narration_seq += 1
        cards.append({"seq": i, "media": row.get("media", ""), "shot_idx": row.get("shot_idx"),
                      "role": row.get("role", ""), "text": row.get("segment_text", ""),
                      "alternatives": row.get("alternatives", []),
                      "frame": target.relative_to(dest).as_posix(), "frame_missing": image is None,
                      "source_fps": fps, "source_fps_assumed": fps_assumed,
                      "source_in_frame": source_in, "source_out_frame": source_out,
                      "source_in_tc": in_tc, "source_out_tc": out_tc,
                      "source_in_seconds": in_seconds, "source_out_seconds": out_seconds,
                      "source_frames_estimated": index is None,
                      "source_timecode_mode": "pts" if index and index.mode == "vfr" else "non-drop-frame",
                      "source_timing_note": timing_note,
                      "source_alignment": alignment,
                      "timeline_in_frame": timeline_in, "timeline_out_frame": timeline_out,
                      "timeline_in_tc": timecode(timeline_in, timeline_fps),
                      "timeline_out_tc": timecode(timeline_out, timeline_fps),
                      "narration_duration": estimate, "picture_duration": duration,
                      "narration_seq": narration_seq if has_narration else None,
                      "shortfall": round(max(0, estimate - duration), 2)})
        for shot in valid_shots(media or {}):
            if shot["start"] < start + duration and shot["end"] > start:
                used.add((media["name"], shot["idx"]))
        elapsed += duration
    available = [(m["name"], s["idx"]) for m in state.get("media", []) for s in valid_shots(m)]
    return {"timeline_fps": timeline_fps, "timecode_mode": "non-drop-frame",
            "out_point": "exclusive", "cards": cards,
            "utilization": {"used_shots": len(used), "total_shots": len(available),
                            "unused": [{"media": m, "shot_idx": idx} for m, idx in available if (m, idx) not in used]}}


def markdown(storyboard: dict) -> list[str]:
    lines = ["## 分镜总览\n", "所有出点均不包含该帧。恒定帧率使用非丢帧 HH:MM:SS:FF；变帧率源素材显示 HH:MM:SS.微秒实际时间戳及解码帧号。",
             "秒数按最近帧边界量化，源时间从首个视频帧起算；未取得逐帧索引时明确标注估算。网络待补素材替换后需重建。",
             f"成片时间轴按 {storyboard['timeline_fps']:g} fps；源素材使用各自 fps。缺失 fps 时明确标注估算。\n"]
    for card in storyboard["cards"]:
        lines += [f"### #{card['seq']} · {card['role']} · {card['media'] or '网络待补'}",
                  f"![分镜 {card['seq']}](<{card['frame']}>)",
                  "画面待人工补充（占位图）。" if card["frame_missing"] else "",
                  "> " + str(card["text"] or "无旁白画面").replace("\n", "\n> "),
                  f"源素材：{card['source_in_tc']} → {card['source_out_tc']}\n"]
        lines += [f"- 备选：{a['media']}#{a['shot_idx']} [{a['start']:.2f}, {a['end']:.2f})s · {a['reason']}"
                  for a in card.get("alternatives", [])]
    lines.append("## 执行卡\n")
    for card in storyboard["cards"]:
        fps_note = "（fps 未知，暂用成片 fps）" if card["source_fps_assumed"] else ""
        lines += [f"### #{card['seq']}",
                  f"- 源入/出：{card['source_in_tc']} → {card['source_out_tc']}；帧 [{card['source_in_frame']}, {card['source_out_frame']})，{card['source_fps']:g} fps{fps_note}",
                  "- " + card.get("source_timing_note", "旧执行卡未记录帧定位依据，请重建后核对。"),
                  f"- 成片/旁白锚点：{card['timeline_in_tc']} → {card['timeline_out_tc']}；帧 [{card['timeline_in_frame']}, {card['timeline_out_frame']})",
                  f"- 画面 {card['picture_duration']:.2f}s；旁白估算 {card['narration_duration']:.2f}s。"]
        lines.append(f"- 对齐旁白第 {card['narration_seq']} 段。" if card.get("narration_seq") else "- 本段无旁白，不占旁白编号。")
        if card["shortfall"] > .05:
            lines.append(f"- ⚠ 画面短于旁白 {card['shortfall']:.2f}s，建议延长/跨镜头/改短旁白。")
    utilization = storyboard["utilization"]
    lines += ["\n## 素材利用率\n", f"使用 {utilization['used_shots']}/{utilization['total_shots']} 个已识别镜头。"]
    lines += [f"- 未使用：{item['media']}#{item['shot_idx']}" for item in utilization["unused"]]
    return lines
