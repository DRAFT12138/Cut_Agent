"""Per-unit stage checkpoints; only completed units are written atomically."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .runctl import read_json, write_json_atomic
from .media import VIDEO_EXT, IMAGE_EXT


def fingerprint(path: Path) -> dict:
    try:
        stat = path.stat()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except FileNotFoundError:
        return {"missing": True}


def media_snapshot(folder: str) -> list[dict]:
    root = Path(folder)
    return [
        {"name": p.relative_to(root).as_posix(), **fingerprint(p)}
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix.lower() in VIDEO_EXT | IMAGE_EXT
        and not any(part.startswith(".") or part == "_thumbs"
                    for part in p.relative_to(root).parts)
    ]


class UnitStore:
    def __init__(self, root: Path | None, stage: str):
        self.root = root / "units" / stage if root else None

    def path(self, key) -> Path | None:
        if self.root is None:
            return None
        digest = hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False,
                                          default=str).encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def read(self, key) -> dict | None:
        path = self.path(key)
        data = read_json(path) if path else None
        return data if isinstance(data, dict) else None

    def write(self, key, value: dict) -> None:
        path = self.path(key)
        if path:
            write_json_atomic(path, value)
