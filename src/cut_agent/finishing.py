"""Deterministic finishing instructions, using the storyboard's exact frame anchors."""
import math

from PIL import Image

from .shots import number
from .storyboard import source_position, source_timing_note
from .source_timing import FrameIndex
from .delivery import PLATFORMS, build_delivery


def music_cues(timeline, storyboard):
    return [{"seq": card["seq"], "frame": card["timeline_in_frame"], "timecode": card["timeline_in_tc"],
             "instruction": "高潮进入：在此附近选择音乐强拍，可适当抬音量，旁白期间保持 ducking"}
            for row, card in zip(timeline, storyboard.get("cards", [])) if row.get("role") == "高潮"]


def cover_candidates(media: list[dict], limit: int = 3) -> list[dict]:
    """Rank measured representative frames; label legacy shot-average fallback."""
    measured, fallback = [], []
    for item in media:
        index = FrameIndex.optional(item.get("source_timing"))
        fps = number(item.get("fps"))
        if index and index.mode == "cfr":
            fps = index.fps
        assumed = fps <= 0
        fps = 25 if assumed else fps
        for shot in item.get("shots", []):
            old_candidate = None
            times, motions = shot.get("frame_times", []), shot.get("frame_motions", [])
            for i, path in enumerate(shot.get("frames", [])):
                try:
                    with Image.open(path) as image:
                        image.verify()
                except (OSError, TypeError, ValueError):
                    continue
                moment = number(times[i], -1) if i < len(times) else -1
                if moment >= 0 and not number(shot.get("start")) <= moment < number(shot.get("end")):
                    continue
                value = number(motions[i], float("nan")) if i < len(motions) else float("nan")
                known = math.isfinite(value) and value >= 0 and moment >= 0
                frame, _, label = source_position(moment, fps, index, allow_end=False) if moment >= 0 else (None, None, None)
                candidate = {"media": item["name"], "shot_idx": shot["idx"], "frame": str(path),
                             "frame_time": moment if moment >= 0 else None,
                             "motion": round(value, 4) if known else None,
                             "shot_motion": number(shot.get("motion")),
                             "motion_source": "frame" if known else "shot-average-fallback",
                             "source_frame": frame, "source_fps": fps, "source_fps_assumed": assumed,
                             "source_timecode": label, "source_frames_estimated": index is None,
                             "source_timecode_mode": "pts" if index and index.mode == "vfr" else "non-drop-frame",
                             "source_timing_note": source_timing_note(index)}
                if known:
                    measured.append(candidate)
                elif old_candidate is None:
                    old_candidate = candidate
            if old_candidate:
                fallback.append(old_candidate)
    def order(candidate):
        value = candidate["motion"] if candidate["motion"] is not None else candidate["shot_motion"]
        return (-value, candidate["media"], candidate["shot_idx"], candidate["frame_time"] or 0)
    selected = sorted(measured, key=order)[:limit]
    selected_shots = {(c["media"], c["shot_idx"]) for c in selected}
    selected += [c for c in sorted(fallback, key=order)
                 if (c["media"], c["shot_idx"]) not in selected_shots][:max(0, limit - len(selected))]
    return selected


def build_guide(state: dict, platform: str, editorial: dict | None = None) -> dict:
    storyboard = state["storyboard"]
    rows = state.get("timeline", [])
    media = {m["name"]: m for m in state.get("media", [])}
    editorial = editorial if isinstance(editorial, dict) else {}
    labels = editorial.get("rows", [])
    by_seq = {item.get("seq"): item for item in labels if isinstance(item, dict) and isinstance(item.get("seq"), int)} if isinstance(labels, list) else {}
    def shot(row):
        return next((s for s in media.get(row.get("media"), {}).get("shots", []) if s.get("idx") == row.get("shot_idx")), {})
    steps = []
    narration_seq = 0
    for index, (row, card) in enumerate(zip(rows, storyboard["cards"]), 1):
        current = shot(row)
        previous = shot(rows[index - 2]) if index > 1 else {}
        if index == 1:
            transition = "直接进入开场画面，先确认前 3 秒钩子"
        elif row.get("role") == "高潮":
            transition = "在标注锚点附近对齐强拍硬切进入高潮"
        elif current and previous and abs(number(current.get("motion")) - number(previous.get("motion"))) > 8:
            transition = "运动度差较大，优先硬切；确有时间跳转时可试短暂淡黑"
        elif row.get("media") and row.get("media") == rows[index - 2].get("media"):
            transition = "同素材连续镜头可试短叠化，先检查动作是否连贯"
        else:
            transition = "先用硬切检查叙事连贯，再决定是否需要转场"
        color = by_seq.get(index, {}).get("color")
        color_source = "model" if isinstance(color, str) and color.strip() else "rule"
        if color_source == "rule":
            color = "对照代表帧统一曝光与白平衡，保护高光和肤色，再微调饱和度"
        if str(row.get("segment_text") or "").strip():
            narration_seq += 1
            voice = f"在成片帧 [{card['timeline_in_frame']}, {card['timeline_out_frame']}) 对齐旁白第 {narration_seq} 段，字幕与实录/TTS 波形复核"
        else:
            voice = f"成片帧 [{card['timeline_in_frame']}, {card['timeline_out_frame']}) 为无旁白画面，不占旁白编号；按需要保留环境声或音乐"
        steps.append({"seq": index, "anchor": card, "transition": transition,
                      "color": color[:500], "color_source": color_source,
                      "voice": voice,
                      "rhythm": f"画面 {card['picture_duration']:.2f}s / 旁白估算 {card['narration_duration']:.2f}s；缺口 {card['shortfall']:.2f}s"})
    covers = cover_candidates(list(media.values()))
    cover_note = "按已有代表帧的帧间运动度选前三；镜头切换的画面跳变不计作镜头内部运动。"
    if any(c["motion_source"] != "frame" for c in covers):
        cover_note += " 部分旧素材无帧级记录，补充候选仅按镜头平均运动度排序，不计作已验证峰值帧。"
    titles = editorial.get("titles")
    if not isinstance(titles, list) or not any(isinstance(t, str) and t.strip() for t in titles):
        titles = [str(state.get("copy", "")).strip().split("\n")[0][:24] or "粗剪方案"]
    delivery = build_delivery(state, platform)
    checklist = [{"checked": False, "text": text} for text in (
        "前 3 秒开场钩子成立", "相邻素材与动作连续性已人工复核", "旁白/字幕与画面按实际音频对齐", "网络素材来源、授权与替换已核对")]
    checklist.extend({"checked": False, "text": item["message"]} for item in state.get("critique", {}).get("warnings", []))
    checklist.extend({"checked": False, "text": item["message"]} for item in delivery["checks"] if item["status"] != "pass")
    return {"steps": steps, "music": {"start_frame": 0, "fade_in_seconds": .5, "fade_out_seconds": 1.5,
                                      "ducking_db": -12, "cues": music_cues(rows, storyboard)},
            "titles": [t[:100] for t in titles if isinstance(t, str) and t.strip()][:3],
            "covers": covers, "cover_selection_note": cover_note,
            "delivery": delivery, "checklist": checklist}


def markdown(guide: dict) -> str:
    lines = ["# 精剪指导\n", "[查看粗剪方案与素材目录](粗剪方案.md)\n", "## 逐段执行清单\n"]
    for step in guide["steps"]:
        a = step["anchor"]
        source = a.get("media") or "待补素材"
        if a.get("shot_idx") is not None:
            source += f" #{a['shot_idx']}"
        narration = " ".join(str(a.get("text") or "").split()) or "无旁白画面"
        lines += [f"### #{step['seq']} · {a['timeline_in_tc']} → {a['timeline_out_tc']}",
                  f"- 取材：{source}", f"- 旁白原文：{narration}",
                  f"- 源入/出：{a['source_in_tc']} → {a['source_out_tc']}（出点不含）",
                  "- " + a.get("source_timing_note", "旧执行卡未记录帧定位依据，请重建后核对。"),
                  "- 转场：" + step["transition"], "- 调色建议：" + step["color"],
                  "- 旁白/字幕：" + step["voice"], "- 节奏：" + step["rhythm"]]
    music = guide["music"]
    lines += ["\n## 音乐执行\n", f"- 从成片帧 0 起，淡入 {music['fade_in_seconds']}s，尾部淡出 {music['fade_out_seconds']}s。",
              f"- 旁白期间建议 sidechain ducking 约 {music['ducking_db']}dB，试听确认可懂度。"]
    lines += [f"- #{cue['seq']} {cue['timecode']}（帧 {cue['frame']}）：{cue['instruction']}" for cue in music["cues"]]
    lines += ["\n## 封面与标题\n"] + ["- 标题建议：" + title for title in guide["titles"]]
    lines.append(guide["cover_selection_note"])
    for c in guide["covers"]:
        score = f"帧运动度 {c['motion']}" if c["motion_source"] == "frame" else f"镜头均值 {c['shot_motion']}，帧峰值未知"
        anchor = c["source_timecode"] or "帧时间未知"
        lines.append(f"- 封面候选：{c['media']}#{c['shot_idx']} · {anchor}（{score}）\n\n![封面候选](<{c['frame']}>)")
        lines.append(f"  源帧 {c.get('source_frame')}；{c.get('source_timing_note', '')}")
    d = guide["delivery"]
    lines += ["\n## 平台交付\n", f"- {d['label']}：{d['width']}×{d['height']}，{d['fps']}fps，建议 {d['bitrate_mbps']}Mbps，目标≤{d['target_max_seconds']}s。",
              "- " + d["subtitle_safe_area"], "- " + d["note"]]
    lines += [f"- {'通过' if item['status'] == 'pass' else '待核对'}：{item['message']}" for item in d["checks"]]
    limits = d["upload_limit"]
    lines.append(f"\n发布限制资料（核对日期 {limits['references_checked_at']}，按适用范围使用）：")
    lines += [f"- [{ref['title']}]({ref['url']}) · {ref['scope']}：{ref['note']}" for ref in limits["references"]]
    lines.append("\n## 质量门槛\n")
    lines += [f"- [{'x' if item.get('checked') else ' '}] {item['text']}" for item in guide["checklist"]]
    return "\n".join(lines) + "\n"
