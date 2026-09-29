"""Deterministic shot-level text/visual index and compound retrieval."""
from __future__ import annotations

import re


def _terms(value) -> set[str]:
    text = " ".join(value) if isinstance(value, list) else str(value or "")
    return {token.casefold() for token in re.findall(r"[\w\u4e00-\u9fff]+", text)}


def build(media: list[dict]) -> list[dict]:
    """Create stable, serializable index records without copying private frames."""
    records = []
    for source in media:
        for shot in source.get("shots", []):
            tags = shot.get("tags", {}) if isinstance(shot.get("tags"), dict) else {}
            records.append({
                "id": f"{source.get('name', '')}#{shot.get('idx')}",
                "media": source.get("name", ""), "shot_idx": shot.get("idx"),
                "start": shot.get("start"), "end": shot.get("end"),
                "description": shot.get("description", ""), "roles": shot.get("roles", []),
                "tags": tags, "evidence": shot.get("tag_evidence", []),
            })
    return records


def search(index: list[dict], query: str = "", *, entities: list[str] | None = None,
           actions: list[str] | None = None, composition: list[str] | None = None,
           limit: int = 5) -> list[dict]:
    """Rank combined semantic/entity/action/composition conditions.

    Explicit filters are required (AND between groups); free-text terms add a
    deterministic score. Ties retain the stable media/shot order.
    """
    wanted = {
        "subjects": _terms(entities or []), "actions": _terms(actions or []),
        "composition": _terms(composition or []),
    }
    query_terms = _terms(query)
    results = []
    for position, record in enumerate(index):
        tags = record.get("tags", {})
        groups = {
            "subjects": _terms(tags.get("subjects", [])),
            "actions": _terms(tags.get("actions", [])),
            "composition": _terms(tags.get("shot_sizes", []) + tags.get("camera_angles", [])
                                  + tags.get("text_regions", [])),
        }
        if any(need and not (need & groups[key]) for key, need in wanted.items()):
            continue
        haystack = _terms(record.get("description")) | _terms(record.get("roles", []))
        haystack |= set().union(*(_terms(values) for values in tags.values())) if tags else set()
        semantic = len(query_terms & haystack)
        confidence = max((float(e.get("confidence", 0)) for e in record.get("evidence", [])
                          if isinstance(e, dict)), default=0.0)
        result = dict(record)
        result["score"] = round(semantic + sum(bool(v) for v in wanted.values()) + confidence / 100, 4)
        results.append((result["score"], position, result))
    results.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in results[:max(0, limit)]]
