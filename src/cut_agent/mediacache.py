"""Cross-run media understanding cache with independently owned frame files."""
from copy import deepcopy
from pathlib import Path
import shutil
import uuid
import math
from PIL import Image

from .checkpoints import UnitStore
from .source_timing import FrameIndex


class MediaCache:
    def __init__(self, root: Path):
        self.root = root
        self.store = UnitStore(root, "media")

    @staticmethod
    def _complete(row: dict) -> bool:
        return bool(row.get("shots")) and all(
            s.get("frames") and isinstance(s.get("description"), str) and s["description"].strip()
            for s in row["shots"])

    @staticmethod
    def _copy_frames(record: dict, folder: Path) -> dict:
        result = deepcopy(record)
        folder.mkdir(parents=True, exist_ok=True)
        for i, shot in enumerate(result["row"].get("shots", [])):
            paths = []
            for j, source in enumerate(shot.get("frames", [])):
                source = Path(source)
                target = folder / f"shot_{i:04d}_{j:04d}{source.suffix}"
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                paths.append(str(target.resolve()))
            shot["frames"] = paths
        return result

    def read(self, key, run_frames: Path) -> dict | None:
        saved = self.store.read(key)
        if not saved or not isinstance(saved.get("row"), dict):
            return None
        shots = saved["row"].get("shots")
        if not isinstance(shots, list) or not shots or any(not isinstance(s, dict) for s in shots):
            return None
        if not self._complete(saved["row"]):
            return None
        try:
            frames = [frame for shot in shots for frame in shot.get("frames", [])]
            if not frames:
                return None
            for frame in frames:
                with Image.open(frame) as image:
                    image.verify()
            return self._copy_frames(saved, run_frames / self.store.path(key).stem)
        except (OSError, TypeError, ValueError):
            return None  # missing/corrupt assets are a cache miss, not a stage failure

    def write(self, key, record: dict) -> None:
        # Failed semantic results are not sticky across runs: a later online run can improve them.
        if record.get("warnings") or not self._complete(record["row"]):
            return
        folder = self.root / "frames" / uuid.uuid4().hex
        saved = self._copy_frames(record, folder)
        self.store.write(key, saved)


class GeometryCache(MediaCache):
    """Model-independent shots/frames, committed before semantic work begins."""

    def __init__(self, root: Path):
        self.root = root / "geometry"
        self.store = UnitStore(self.root, "geometry")

    @staticmethod
    def _complete(row: dict) -> bool:
        return bool(row.get("shots")) and all(s.get("frames") for s in row["shots"])

    def write(self, key, record: dict) -> None:
        record = deepcopy(record)
        row = record["row"]
        if not row.get("shots") or not all(s.get("frames") for s in row["shots"]):
            return
        # Flattened frame arrays would point at the original run; rebuild them
        # from the copied per-shot frames on read. Never save model text here.
        row.pop("frames", None)
        row.pop("frame_times", None)
        row["description"] = ""
        for shot in row["shots"]:
            shot["description"], shot["roles"] = "", []
        super().write(key, record)


class ProbeCache:
    """Source metadata, scene detection and scan thumbnails; no model dependency."""

    def __init__(self, root: Path):
        self.root = root / "probe"
        self.store = UnitStore(self.root, "probe")

    def read(self, key) -> dict | None:
        record = self.store.read(key)
        if not record or record.get("kind") not in ("video", "image"):
            return None
        try:
            if any(not math.isfinite(record[k]) or record[k] < 0
                   for k in ("duration", "width", "height", "fps", "size_mb")):
                return None
            if not isinstance(record["scene_cuts"], list) or not isinstance(record["thumbnails"], list):
                return None
            if record["kind"] == "video" and not record["thumbnails"]:
                return None
            if record["kind"] == "video" and FrameIndex.optional(record.get("source_timing")) is None:
                return None
            for path in record["thumbnails"]:
                with Image.open(path) as image:
                    image.verify()
        except (KeyError, OSError, TypeError, ValueError):
            return None
        return record

    def frame_directory(self) -> Path:
        # Concurrent runs can compute a cold source without sharing writable files.
        directory = self.root / "frames" / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def write(self, key, record: dict) -> None:
        self.store.write(key, record)
