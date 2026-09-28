"""Deterministic shot selection and source bounds for rough-cut rows."""
from __future__ import annotations

import math

from .source_timing import align_source


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def valid_shots(media: dict) -> list[dict]:
    duration = number(media.get("duration"))
    result = []
    for shot in media.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        start, end = number(shot.get("start"), -1), number(shot.get("end"), -1)
        if 0 <= start < end <= duration + .05:
            result.append({**shot, "start": start, "end": min(duration, end)})
    return sorted(result, key=lambda shot: shot["start"])


def bind_local_source(row: dict, media: dict) -> dict:
    """Keep editorial fields while removing bindings owned by a downloaded asset."""
    result = dict(row)
    for field in ("asset_status", "local_path", "ref", "thumbnail", "source_url", "source_fps", "source_timing"):
        result.pop(field, None)
    if row.get("source", "local") != "local" or row.get("media") != media["name"]:
        result.pop("source_alignment", None)
    result.update(kind=media["kind"], source="local", media=media["name"], needs_web=False)
    return result


def select_shot(row: dict, media: dict) -> dict:
    """Real shot rows use a full shot by default; explicit spans use contiguous shots.

    Legacy/no-vision media retain bounded file+offset behavior and carry shot_idx=None.
    """
    result = bind_local_source(row, media)
    duration = max(0.0, number(media.get("duration")))
    requested = max(.04, number(row.get("use_duration"), 6.0))
    shots = valid_shots(media) if media["kind"] == "video" else []
    if shots:
        wanted = number(row.get("shot_idx"), -1)
        chosen = next((s for s in shots if number(s.get("idx"), -2) == wanted), None)
        if chosen is None:
            offset = number(row.get("start_offset"))
            chosen = min(shots, key=lambda s: abs(s["start"] - offset))
            message = "未提供有效镜头编号，已对齐最近镜头起点"
            note = str(result.get("note") or "")
            result["note"] = note if message in note else "；".join(filter(None, [note, message]))
        chosen_index = shots.index(chosen)
        last = chosen
        span = row.get("span") is True
        if span:
            for shot in shots[chosen_index + 1:chosen_index + 3]:
                if last["end"] - chosen["start"] >= requested or abs(shot["start"] - last["end"]) > .05:
                    break
                last = shot
        result.update(shot_idx=chosen["idx"], start_offset=chosen["start"],
                      start=chosen["start"], end=last["end"],
                      use_duration=round(last["end"] - chosen["start"], 4),
                      span=last is not chosen, shot_end_idx=last["idx"],
                      shot_description=chosen.get("description") or media.get("description", ""))
    else:
        offset = min(max(0.0, number(row.get("start_offset"))), max(0.0, duration - min(requested, duration)))
        use = min(requested, duration - offset) if media["kind"] == "video" else requested
        result.update(shot_idx=None, span=False, start_offset=offset if media["kind"] == "video" else 0,
                      use_duration=use)
        result.update(start=result["start_offset"], end=result["start_offset"] + use)
    return align_source(result, media)


def shot_inventory(media: list[dict]) -> list[str]:
    lines = []
    for item in media:
        shots = valid_shots(item)
        if item["kind"] == "video" and shots:
            for shot in shots:
                lines.append(f"- {item['name']}#{shot['idx']} [video] "
                             f"[{shot['start']:.2f}-{shot['end']:.2f}s] motion={shot.get('motion', 0)} "
                             f"{shot.get('description') or item.get('description') or '未识别画面'} "
                             f"适合角色={','.join(shot.get('roles', []))}")
        else:
            lines.append(f"- {item['name']} [{item['kind']}] 时长={item.get('duration', 0)}s "
                         f"{item.get('description', '')}（无镜头标注，使用文件+offset）")
    return lines
