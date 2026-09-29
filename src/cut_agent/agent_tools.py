"""Stable file-based tools for coding agents that replace the configured LLM."""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import graph
from .runctl import RunError

DECISION_KEYS = ("segments", "media_descriptions", "timeline", "music")


def build_context(media_dir: str, copy: str) -> dict:
    """Probe local media and return the compact context an external agent needs."""
    root = Path(media_dir).expanduser().resolve()
    if not root.is_dir():
        raise RunError(f"素材目录不存在: {root}")
    state = {"media_folder": str(root), "copy": copy, "log": []}
    scanned = graph.scan_media(state)
    return {
        "schema_version": 1,
        "media_dir": str(root),
        "copy": copy,
        "media": scanned["media"],
        "decision_contract": {
            "segments": "ordered narration segments with text, duration, mood, kw_cn and kw_en",
            "media_descriptions": "optional object mapping an exact media filename to a visual description",
            "timeline": "ordered rows with media, segment_text, use_duration, start_offset and needs_web",
            "music": "object with mood, primary and alternatives",
        },
    }


def load_decisions(path: str | Path, media_names: set[str] | None = None) -> dict:
    """Load and validate decisions before they can influence a run."""
    source = Path(path).expanduser().resolve()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError(f"无法读取 agent decisions: {exc}") from exc
    if not isinstance(data, dict):
        raise RunError("agent decisions 顶层必须是 JSON object")
    unknown = set(data) - set(DECISION_KEYS) - {"schema_version"}
    if unknown:
        raise RunError("agent decisions 包含未知字段: " + ", ".join(sorted(unknown)))
    if data.get("schema_version", 1) != 1:
        raise RunError("不支持的 agent decisions schema_version")
    for key in ("segments", "timeline"):
        if not isinstance(data.get(key), list) or not data[key]:
            raise RunError(f"agent decisions.{key} 必须是非空数组")
    segment_texts = set()
    for index, segment in enumerate(data["segments"], 1):
        if not isinstance(segment, dict) or not str(segment.get("text", "")).strip():
            raise RunError(f"segments[{index}] 缺少非空 text")
        duration = segment.get("duration")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration <= 0:
            raise RunError(f"segments[{index}].duration 必须是正数")
        segment_texts.add(segment["text"])
    for index, row in enumerate(data["timeline"], 1):
        if not isinstance(row, dict):
            raise RunError(f"timeline[{index}] 必须是 object")
        if row.get("segment_text") not in segment_texts:
            raise RunError(f"timeline[{index}].segment_text 必须精确引用 segments.text")
        duration = row.get("use_duration")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration <= 0:
            raise RunError(f"timeline[{index}].use_duration 必须是正数")
        offset = row.get("start_offset", 0)
        if not isinstance(offset, (int, float)) or isinstance(offset, bool) or not math.isfinite(offset) or offset < 0:
            raise RunError(f"timeline[{index}].start_offset 必须是非负数")
    if "music" in data and not isinstance(data["music"], dict):
        raise RunError("agent decisions.music 必须是 object")
    descriptions = data.get("media_descriptions", {})
    if not isinstance(descriptions, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in descriptions.items()):
        raise RunError("agent decisions.media_descriptions 必须是 filename 到 description 的字符串映射")
    if media_names is not None:
        referenced = {str(row.get("media")) for row in data["timeline"] if isinstance(row, dict) and row.get("media")}
        missing = referenced - media_names
        if missing:
            raise RunError("时间线引用了不存在的素材: " + ", ".join(sorted(missing)))
    return {key: data[key] for key in DECISION_KEYS if key in data}
