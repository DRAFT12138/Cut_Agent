"""Real narration timing import and deterministic text-to-audio binding.

Speech recognition is deliberately adapter-neutral: callers provide word/phrase
timestamps from their recognizer (or a corrected sidecar).  This module validates
them, records an explicit degraded result when they are unusable, and turns them
into the single timing source consumed by timeline planning.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import subprocess

from .media import _ffprobe


VERSION = 1
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def import_source(path: str | Path) -> dict:
    """Inspect an immutable narration source without copying or modifying it."""
    source = Path(path).resolve()
    if source.suffix.lower() not in AUDIO_EXTENSIONS | VIDEO_EXTENSIONS:
        raise ValueError("旁白仅支持 WAV、MP3、M4A 或含音轨的视频")
    if not source.is_file():
        raise ValueError(f"旁白文件不存在: {source}")
    result = subprocess.run(
        [_ffprobe(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise ValueError("无法读取旁白: " + result.stderr[-160:])
    data = json.loads(result.stdout or "{}")
    stream = next((row for row in data.get("streams", []) if row.get("codec_type") == "audio"), None)
    if stream is None:
        raise ValueError("文件不包含旁白音轨")
    duration = float(stream.get("duration") or data.get("format", {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("旁白时长无效")
    return {"version": VERSION, "path": str(source), "sha256": _fingerprint(source),
            "duration": duration, "sample_rate": int(stream.get("sample_rate") or 0),
            "channels": int(stream.get("channels") or 0), "codec": stream.get("codec_name", "")}


def _units(text: str) -> list[str]:
    # Character units make Chinese alignment deterministic; alphanumeric words
    # remain whole so recognizer output such as "Cut Agent" can also align.
    return re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+", text.casefold())


def timing(source: dict, words: list[dict] | None) -> dict:
    """Validate recognizer output and annotate linguistic cut boundaries."""
    duration = float(source["duration"])
    if not words:
        return {"version": VERSION, "status": "unavailable", "source": source,
                "reason": "未提供词级识别结果；已明确降级为文案估时"}
    normalized = []
    previous = 0.0
    try:
        for index, raw in enumerate(words):
            text = str(raw["text"])
            start, end = float(raw["start"]), float(raw["end"])
            confidence = float(raw.get("confidence", 0))
            if (not text.strip() or not all(map(math.isfinite, (start, end, confidence)))
                    or start < previous or end <= start or end > duration + 1e-3
                    or not 0 <= confidence <= 1):
                raise ValueError(f"第 {index + 1} 个词的时间或置信度无效")
            pause = start - previous
            normalized.append({"text": text, "start": start, "end": end, "confidence": confidence,
                               "pause_before": round(pause, 6),
                               "boundary": "breath" if pause >= .7 else "pause" if pause >= .3
                               else "sentence" if re.search(r"[。！？!?][”’\"']?$", text) else "word",
                               "emphasis": bool(raw.get("emphasis", False))})
            previous = end
    except (KeyError, TypeError, ValueError) as exc:
        return {"version": VERSION, "status": "unavailable", "source": source, "reason": str(exc)}
    return {"version": VERSION, "status": "ready", "source": source, "words": normalized,
            "mean_confidence": sum(row["confidence"] for row in normalized) / len(normalized)}


def bind_segments(segments: list[dict], word_timing: dict) -> list[dict]:
    """Bind lossless narration segments to contiguous real-audio intervals."""
    if word_timing.get("status") != "ready":
        return segments
    words = word_timing["words"]
    word_units = [_units(row["text"]) for row in words]
    flat = [(unit, index) for index, units in enumerate(word_units) for unit in units]
    wanted = [unit for segment in segments for unit in _units(str(segment.get("text", "")))]
    if [unit for unit, _ in flat] != wanted:
        raise ValueError("词级识别文本与旁白分段不一致，不能静默错位")
    result, cursor = [], 0
    duration = float(word_timing["source"]["duration"])
    interval_start = 0.0
    for index, segment in enumerate(segments):
        count = len(_units(str(segment.get("text", ""))))
        selected = flat[cursor:cursor + count]
        if not selected:
            raise ValueError("旁白分段不得为空")
        first, last = selected[0][1], selected[-1][1]
        start = interval_start
        # Give inter-segment silence to the preceding phrase.  Therefore the
        # complete set of intervals is contiguous and exactly covers the track.
        next_word = flat[cursor + count][1] if cursor + count < len(flat) else None
        end = duration if next_word is None else words[next_word]["start"]
        row = dict(segment)
        row.update(duration=round(end - start, 6), narration_start=round(start, 6),
                   narration_end=round(end, 6), timing_source="word", timing_confidence=round(
                       sum(words[n]["confidence"] for n in range(first, last + 1)) / (last - first + 1), 6),
                   preferred_cuts=[words[n]["end"] for n in range(first, last + 1)
                                   if words[n]["boundary"] in {"pause", "breath", "sentence"}])
        result.append(row)
        interval_start = end
        cursor += count
    return result
