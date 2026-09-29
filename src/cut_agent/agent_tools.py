"""Stable file-based tools for coding agents that replace the configured LLM."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

from . import graph
from .runctl import RunError

SCHEMA_VERSION = 2
DECISION_KEYS = ("segments", "media_descriptions", "timeline", "music")


def media_id(name: str) -> str:
    """Return a short, deterministic identifier for a media-relative path."""
    return "media_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def _thumbnail_samples(item: dict) -> list[dict]:
    if item.get("kind") != "video":
        return [{"path": path, "time": None} for path in item.get("thumbnails", [])]
    duration = float(item.get("duration") or 0)
    samples = []
    for path in item.get("thumbnails", []):
        match = re.search(r"_t([123])\.jpg$", str(path))
        timestamp = duration * int(match.group(1)) / 4 if match else None
        samples.append({"path": path, "time": round(timestamp, 3) if timestamp is not None else None})
    return samples


def agent_media(items: list[dict]) -> list[dict]:
    """Add stable IDs and timestamped observations to scanned media records."""
    result = []
    for source in items:
        item = dict(source)
        item["media_id"] = media_id(str(item["name"]))
        item["thumbnail_samples"] = _thumbnail_samples(item)
        item["max_use_duration"] = float(item.get("duration") or 0) if item.get("kind") == "video" else None
        result.append(item)
    return result


def build_context(media_dir: str, copy: str) -> dict:
    """Probe local media and return the compact context an external agent needs."""
    root = Path(media_dir).expanduser().resolve()
    if not root.is_dir():
        raise RunError(f"素材目录不存在: {root}")
    state = {"media_folder": str(root), "copy": copy, "log": []}
    scanned = graph.scan_media(state)
    return {
        "schema_version": SCHEMA_VERSION,
        "media_dir": str(root),
        "copy": copy,
        "media": agent_media(scanned["media"]),
        "decision_contract": {
            "segments": "ordered narration segments with unique segment_id, exact text, duration, mood and keywords",
            "media_descriptions": "optional object mapping media_id to a visual description",
            "timeline": "one row per segment_id using media_id, use_duration, start_offset and needs_web",
            "music": "object with mood, primary and alternatives",
        },
    }


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def load_decisions(path: str | Path, media: list[dict] | set[str] | None = None,
                   copy: str | None = None) -> dict:
    """Load, validate and normalize decisions before they influence a run."""
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
    version = data.get("schema_version", 1)
    if version not in (1, SCHEMA_VERSION):
        raise RunError(f"不支持的 agent decisions schema_version: {version}")
    for key in ("segments", "timeline"):
        if not isinstance(data.get(key), list) or not data[key]:
            raise RunError(f"agent decisions.{key} 必须是非空数组")

    segment_by_id = {}
    for index, segment in enumerate(data["segments"], 1):
        if not isinstance(segment, dict) or not str(segment.get("text", "")).strip():
            raise RunError(f"segments[{index}] 缺少非空 text")
        duration = segment.get("duration")
        if not _positive_number(duration):
            raise RunError(f"segments[{index}].duration 必须是正数")
        identifier = segment.get("segment_id") or (f"segment_{index:03d}" if version == 1 else None)
        if not isinstance(identifier, str) or not identifier.strip():
            raise RunError(f"segments[{index}].segment_id 必须是非空字符串")
        if identifier in segment_by_id:
            raise RunError(f"重复的 segment_id: {identifier}")
        segment["segment_id"] = identifier
        segment_by_id[identifier] = segment
    if copy is not None and _compact("".join(segment["text"] for segment in data["segments"])) != _compact(copy):
        raise RunError("segments.text 按顺序拼接后必须完整覆盖原始文案，且不得改写")

    media_by_id, media_by_name = _media_indexes(media)
    used_segments = []
    for index, row in enumerate(data["timeline"], 1):
        if not isinstance(row, dict):
            raise RunError(f"timeline[{index}] 必须是 object")
        identifier = row.get("segment_id")
        if identifier is None and version == 1:
            text = row.get("segment_text")
            identifier = next((key for key, segment in segment_by_id.items() if segment["text"] == text), None)
        if identifier not in segment_by_id:
            raise RunError(f"timeline[{index}].segment_id 必须精确引用 segments.segment_id")
        row["segment_id"] = identifier
        row["segment_text"] = segment_by_id[identifier]["text"]
        used_segments.append(identifier)
        if not _positive_number(row.get("use_duration")):
            raise RunError(f"timeline[{index}].use_duration 必须是正数")
        offset = row.get("start_offset", 0)
        if not _nonnegative_number(offset):
            raise RunError(f"timeline[{index}].start_offset 必须是非负数")
        _normalize_media_row(row, index, media_by_id, media_by_name)
    counts = Counter(used_segments)
    missing = [identifier for identifier in segment_by_id if counts[identifier] == 0]
    repeated = [identifier for identifier, count in counts.items() if count > 1]
    if missing or repeated:
        details = (["缺少 " + ", ".join(missing)] if missing else []) + (["重复 " + ", ".join(repeated)] if repeated else [])
        raise RunError("timeline 必须恰好覆盖每个 segment_id 一次：" + "；".join(details))

    if "music" in data and not isinstance(data["music"], dict):
        raise RunError("agent decisions.music 必须是 object")
    descriptions = data.get("media_descriptions", {})
    if not isinstance(descriptions, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in descriptions.items()):
        raise RunError("agent decisions.media_descriptions 必须是 media_id 到 description 的字符串映射")
    normalized_descriptions = {}
    for key, value in descriptions.items():
        item = media_by_id.get(key) or media_by_name.get(key)
        if media is not None and item is None:
            raise RunError(f"media_descriptions 引用了不存在的素材: {key}")
        normalized_descriptions[item["name"] if item else key] = value
    data["media_descriptions"] = normalized_descriptions
    return {key: data[key] for key in DECISION_KEYS if key in data}


def _positive_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _nonnegative_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _media_indexes(media: list[dict] | set[str] | None) -> tuple[dict, dict]:
    if media is None:
        return {}, {}
    records = ([{"name": name, "media_id": media_id(name)} for name in media]
               if isinstance(media, set) else agent_media(media))
    return ({item["media_id"]: item for item in records}, {item["name"]: item for item in records})


def _normalize_media_row(row: dict, index: int, by_id: dict, by_name: dict) -> None:
    reference = row.get("media_id") or row.get("media") or ""
    if not reference:
        if row.get("needs_web") is not True:
            raise RunError(f"timeline[{index}] 未指定本地素材时 needs_web 必须为 true")
        row.update(media="", media_id="", start_offset=0)
        return
    item = by_id.get(reference) or by_name.get(reference)
    if (by_id or by_name) and item is None:
        raise RunError(f"timeline[{index}] 引用了不存在的素材: {reference}")
    if item is None:
        row.setdefault("media", reference)
        return
    row.update(media_id=item["media_id"], media=item["name"], needs_web=False)
    if item.get("kind") == "image":
        row["start_offset"] = 0
        return
    duration = float(item.get("duration") or 0)
    end = float(row.get("start_offset", 0)) + float(row["use_duration"])
    if end > duration + 1e-6:
        raise RunError(f"timeline[{index}] 超出 {item['name']} 时长：出点 {end:.3f}s > {duration:.3f}s")
