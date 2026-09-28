"""生成演示用素材（testsrc 视频 + 几张大图），写入 sample_media/。

运行：python -m make_sample   （或 python make_sample.py）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from cut_agent.media import _ffmpeg  # noqa: E402
from cut_agent.config import SAMPLE_MEDIA  # noqa: E402


def _run(args: list[str]) -> None:
    r = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise SystemExit(f"ffmpeg 失败: {r.stderr[-400:]}")


def main() -> None:
    SAMPLE_MEDIA.mkdir(parents=True, exist_ok=True)
    ff = _ffmpeg()

    # 1) 三段 8s 测试视频（不同颜色/图案，模拟不同场景）
    for i, (name, pattern) in enumerate([
        ("city_night.mp4", "testsrc2=size=1280x720:rate=30"),
        ("mountain_day.mp4", "smptebars=size=1280x720:rate=30"),
        ("ocean_waves.mp4", "mandelbrot=size=1280x720:rate=30"),
    ], 1):
        out = SAMPLE_MEDIA / name
        if not out.exists():
            print(f"生成 {name} ...")
            _run([ff, "-hide_banner", "-loglevel", "error", "-y",
                  "-f", "lavfi", "-i", pattern,
                  "-pix_fmt", "yuv420p", "-t", "8", str(out)])

    # 2) 三张大图
    for i, (name, color) in enumerate([
        ("beach.jpg", "0x2288cc"), ("forest.jpg", "0x228844"), ("sunset.jpg", "0xcc6622"),
    ], 1):
        out = SAMPLE_MEDIA / name
        if not out.exists():
            print(f"生成 {name} ...")
            _run([ff, "-hide_banner", "-loglevel", "error", "-y",
                  "-f", "lavfi", "-i", f"color=c={color}:s=1920x1080:d=1:r=1",
                  "-frames:v", "1", str(out)])

    print(f"\n已就绪 {SAMPLE_MEDIA}:")
    for p in sorted(SAMPLE_MEDIA.iterdir()):
        if p.is_file() and p.suffix.lower() in (".mp4", ".jpg"):
            print(f"  {p.name}  {p.stat().st_size//1024} KB")


if __name__ == "__main__":
    main()
