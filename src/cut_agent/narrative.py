"""Deterministic narrative structure for narration planning.

The model may suggest boundaries and labels, but this module owns the invariant
that the original copy is represented exactly once and in its original order.
"""
from __future__ import annotations

from copy import deepcopy
import re


ROLES = ("钩子", "铺垫", "证据", "转折", "高潮", "收束")
_BREAK = re.compile(r"(?<=[。！？!?；;])|\n+")
_ENTITY = re.compile(r"(?:[A-Za-z][A-Za-z0-9_-]{1,}|\d+(?:\.\d+)?%?|[\u4e00-\u9fff]{2,8})")


def _fallback_parts(copy: str) -> list[str]:
    """Split on language boundaries while retaining every source character."""
    parts, start = [], 0
    for match in _BREAK.finditer(copy):
        if match.end() > start:
            parts.append(copy[start:match.end()])
        start = match.end()
    if start < len(copy):
        parts.append(copy[start:])
    merged = []
    for part in parts:
        if part.isspace() and merged:
            merged[-1] += part
        else:
            merged.append(part)
    return merged or ([copy] if copy else [])


def _model_parts_are_lossless(copy: str, segments: list[dict]) -> bool:
    return bool(segments) and "".join(str(row.get("text", "")) for row in segments) == copy


def _role(index: int, total: int, text: str, proposed: str = "") -> str:
    if proposed in ROLES:
        return proposed
    if index == 0:
        return "钩子"
    if index == total - 1:
        return "收束"
    if re.search(r"但是|然而|却|不过|转而|没想到", text):
        return "转折"
    if re.search(r"数据|例如|因为|证明|显示|调查|研究", text):
        return "证据"
    if index >= max(1, total - 2):
        return "高潮"
    return "铺垫"


def _sentences(text: str) -> list[dict]:
    values = _fallback_parts(text)
    return [{"sentence_id": f"sentence_{i:03d}", "text": value}
            for i, value in enumerate(values, 1)]


def plan(copy: str, segments: list[dict]) -> tuple[list[dict], dict]:
    """Return lossless semantic segments and a whole-film narrative outline."""
    source = deepcopy(segments)
    if not _model_parts_are_lossless(copy, source):
        source = [{"text": text} for text in _fallback_parts(copy)]
    total = len(source)
    result = []
    for index, row in enumerate(source):
        text = str(row.get("text", ""))
        role = _role(index, total, text, str(row.get("narrative_role", "")))
        compact = re.sub(r"\s", "", text)
        entities = list(dict.fromkeys(_ENTITY.findall(text)))[:8]
        item = {**row,
                "segment_id": str(row.get("segment_id") or f"segment_{index + 1:03d}"),
                "narrative_role": role,
                "information_density": round(len(entities) / max(1, len(compact)) * 10, 3),
                "emotion": str(row.get("emotion") or row.get("mood") or "中性"),
                "visual_goal": str(row.get("visual_goal") or f"用可识别画面表达：{compact[:24]}"),
                "required_entities": row.get("required_entities") or entities,
                "acceptable_alternatives": row.get("acceptable_alternatives") or [],
                "sentences": _sentences(text)}
        result.append(item)

    chapters, current = [], None
    for item in result:
        phase = "开场" if item["narrative_role"] == "钩子" else "结尾" if item["narrative_role"] == "收束" else "主体"
        if current is None or current["title"] != phase:
            current = {"chapter_id": f"chapter_{len(chapters) + 1:03d}", "title": phase,
                       "segment_ids": [], "purpose": "建立注意" if phase == "开场" else "完成回收" if phase == "结尾" else "推进信息"}
            chapters.append(current)
        current["segment_ids"].append(item["segment_id"])

    suggestions = []
    for index, item in enumerate(result):
        length = len(re.sub(r"\s", "", item["text"]))
        if length < 6 and index + 1 < total:
            suggestions.append({"type": "merge", "segment_ids": [item["segment_id"], result[index + 1]["segment_id"]],
                                "reason": "语义段过碎（少于 6 字）"})
        elif length > 80:
            suggestions.append({"type": "split", "segment_ids": [item["segment_id"]],
                                "reason": "语义段过长（超过 80 字）"})
        if item["text"] and not re.search(r"[。！？!?；;\n]$", item["text"]) and index + 1 < total:
            suggestions.append({"type": "boundary_review", "segment_ids": [item["segment_id"]],
                                "reason": "分段未落在明确语言边界"})
    outline = {"schema_version": 1, "source_text": copy, "chapters": chapters,
               "segment_order": [item["segment_id"] for item in result], "suggestions": suggestions}
    return result, outline
