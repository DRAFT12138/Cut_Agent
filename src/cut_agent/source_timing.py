"""Native decoded-frame indices. Source seconds start at the first video frame."""
from __future__ import annotations

from bisect import bisect_left
from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess


VERSION = 1


class FrameIndex:
    def __init__(self, record: dict):
        if record.get("version") != VERSION or record.get("status") != "ready":
            raise ValueError("逐帧索引不可用")
        self.base = float(Fraction(record["time_base"]))
        self.origin = record["origin_pts"]
        self.count = record["frame_count"]
        self.end = record["end_pts"]
        self.mode = record["mode"]
        self.fps = float(Fraction(record["frame_rate"]))
        if (not math.isfinite(self.base) or self.base <= 0 or not math.isfinite(self.fps) or self.fps <= 0
                or type(self.count) is not int or self.count <= 0 or type(self.end) is not int
                or type(record["origin_pts"]) is not int or self.mode not in ("cfr", "vfr")):
            raise ValueError("逐帧索引格式无效")
        step = record.get("step_pts")
        if step is not None:
            if type(step) is not int or step <= 0:
                raise ValueError("逐帧步长无效")
            self.ticks = range(0, self.count * step, step)
        else:
            self.ticks = record["pts"]
        if (len(self.ticks) != self.count or self.ticks[0] != 0
                or any(type(t) is not int for t in self.ticks)
                or any(a >= b for a, b in zip(self.ticks, self.ticks[1:]))
                or self.end <= self.ticks[-1]):
            raise ValueError("逐帧时间戳必须完整、严格递增")
        if self.mode == "cfr" and (any(abs(p * self.base - i / self.fps) > self.base * .51
                                       for i, p in enumerate(self.ticks))
                                   or abs(self.end * self.base - self.count / self.fps) > self.base * .51):
            raise ValueError("恒定帧率标记与实际时间戳不一致")

    @classmethod
    def optional(cls, record):
        try:
            return cls(record)
        except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError):
            return None

    def seconds(self, frame: int) -> float:
        return (self.end if frame == self.count else self.ticks[frame]) * self.base

    def nearest(self, seconds: float, *, allow_end: bool = True) -> int:
        target = seconds / self.base
        position = bisect_left(self.ticks, target)
        last = self.count if allow_end else self.count - 1
        candidates = {min(last, position), min(last, max(0, position - 1))}
        return min(candidates, key=lambda n: (round(abs((self.end if n == self.count else self.ticks[n]) - target), 7), -n))


def align_source(row: dict, media: dict | None = None) -> dict:
    """Resolve a requested video range to whole native frames, without decoding."""
    result = dict(row)
    source = row if row.get("source") == "web" else media or {}
    index = FrameIndex.optional(source.get("source_timing"))
    if source.get("kind", row.get("kind")) != "video" or index is None:
        result.pop("source_alignment", None)
        return result
    try:
        start, duration = float(row.get("start_offset", 0)), float(row["use_duration"])
    except (TypeError, ValueError, KeyError):
        return result  # Normal validation still reports invalid user input.
    tolerance = max(1e-6, index.base * .51)
    if (not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration <= 0
            or start >= index.seconds(index.count) or start + duration > index.seconds(index.count) + tolerance):
        return result  # Never repair an out-of-bounds request by silently clamping it.
    first = index.nearest(start, allow_end=False)
    end = max(first + 1, index.nearest(start + duration))
    resolved_start, resolved_end = index.seconds(first), index.seconds(end)
    resolved_duration = resolved_end - resolved_start
    previous = result.pop("source_alignment", None)
    if not (math.isclose(start, resolved_start, abs_tol=1e-9, rel_tol=0)
            and math.isclose(duration, resolved_duration, abs_tol=1e-9, rel_tol=0)):
        result["source_alignment"] = {"requested_start": start, "requested_duration": duration,
            "start": resolved_start, "duration": resolved_duration, "in_frame": first, "out_frame": end}
    elif (isinstance(previous, dict) and previous.get("in_frame") == first and previous.get("out_frame") == end
          and previous.get("start") == resolved_start and previous.get("duration") == resolved_duration):
        result["source_alignment"] = previous  # Re-export retains the original adjustment disclosure.
    result.update(start_offset=resolved_start, start=resolved_start, end=resolved_end, use_duration=resolved_duration)
    return result


def align_timeline(timeline: list[dict], media: list[dict]) -> list[dict]:
    by_name = {item["name"]: item for item in media}
    return [align_source(row, by_name.get(row.get("media"))) for row in timeline]


def probe_timing(path: Path, stream: dict, duration: float) -> dict:
    """Read actual frame PTS once during scanning; keep compact CFR records."""
    from .media import _ffprobe
    try:
        result = subprocess.run(
            [_ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_frames",
             "-show_entries", "frame=best_effort_timestamp,duration,pkt_duration",
             "-of", "json", str(path)], capture_output=True, text=True,
            timeout=max(120, duration * 4),
        )
        if result.returncode or result.stderr.strip():
            raise ValueError("帧时间戳解码失败：" + result.stderr[-160:])
        frames = json.loads(result.stdout)["frames"]
        pts = [int(f["best_effort_timestamp"]) for f in frames]
        origin = pts[0]
        pts = [p - origin for p in pts]
        last_duration = int(frames[-1].get("duration") or frames[-1].get("pkt_duration") or 0)
        if last_duration <= 0:
            # Use the video stream's end only; container duration may come from audio.
            stream_start = stream.get("start_pts")
            last_duration = int(stream.get("duration_ts") or 0) + int(stream_start if stream_start is not None else origin) - origin - pts[-1]
        if last_duration <= 0:
            raise ValueError("末帧缺少有效时长，无法确认源出点")
        base = Fraction(stream["time_base"])
        rate = Fraction(stream.get("avg_frame_rate") or "0")
        if rate <= 0:
            rate = Fraction(stream.get("r_frame_rate") or "0")
        if rate <= 0:
            rate = 1 / (last_duration * base)
        end = pts[-1] + last_duration
        uniform = all(b - a == last_duration for a, b in zip(pts, pts[1:]))
        if uniform:
            rate = 1 / (last_duration * base)
        # Coarse container time bases may round CFR PTS by half a tick.
        cfr = all(abs(float(p * base - Fraction(i, 1) / rate)) <= float(base) * .51
                  for i, p in enumerate([*pts, end]))
        record = {"version": VERSION, "status": "ready", "time_base": str(base),
                  "origin_pts": origin, "frame_count": len(pts), "end_pts": end,
                  "mode": "cfr" if cfr else "vfr", "frame_rate": str(rate)}
        if uniform:
            record["step_pts"] = last_duration
        else:
            record["pts"] = pts
        FrameIndex(record)  # Reject missing, duplicate or nonmonotonic source timestamps.
        return record
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError,
            ZeroDivisionError) as exc:
        return {"version": VERSION, "status": "unavailable", "error": str(exc)[:240]}


def timestamp(seconds: float) -> str:
    microseconds = max(0, round(seconds * 1_000_000))
    seconds, fraction = divmod(microseconds, 1_000_000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{fraction:06d}"
