"""Compare exact-frame preview against full decoding on a five-minute source."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from cut_agent import media
from cut_agent.render import _check_video, normalize_segment


def decoded_sha256(path: Path) -> str:
    raw = subprocess.run(
        [media._ffmpeg(), "-v", "error", "-i", str(path),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return hashlib.sha256(raw).hexdigest()


def main() -> None:
    folder = Path("work/verification") / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:4])
    folder.mkdir(parents=True)
    source = folder / "source.mp4"
    subprocess.run(
        [media._ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=300",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
         "-pix_fmt", "yuv420p", str(source)], check=True,
    )
    timing = media._probe_video(source, folder).source_timing
    assert timing["status"] == "ready" and timing["frame_count"] == 9000
    measurements = []
    for label, first in (("early", 150), ("late", 8700)):
        digests = []
        for approach in ("full", "indexed_seek"):
            output = folder / f"{label}-{approach}.mp4"
            started = time.perf_counter()
            normalize_segment(source, "video", first / 30, 10 / 30, output,
                              fps=30, source_frames=[first, first + 10],
                              source_timing=timing if approach == "indexed_seek" else None)
            elapsed = time.perf_counter() - started
            _check_video(output, 10, 30, label)
            digest = decoded_sha256(output)
            digests.append(digest)
            measurements.append({"position": label, "first_source_frame": first,
                                 "approach": approach, "render_seconds": round(elapsed, 3),
                                 "decoded_sha256": digest})
        assert digests[0] == digests[1], f"decoded frames differ at {label}"
    report = {"source": str(source.resolve()), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "source_frames": timing["frame_count"], "source_duration_seconds": 300,
              "output_frames_each": 10, "decoded_frames_equal": True, "measurements": measurements}
    path = folder / "long-seek-verification.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(path.resolve())
    print(json.dumps(measurements, indent=2))


if __name__ == "__main__":
    main()
