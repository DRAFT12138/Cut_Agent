"""Verify fractional source ranges through the real storyboard, guide and preview.

uv run --no-sync python tools/verify_frame_alignment.py
"""
import hashlib
import json
from pathlib import Path
import subprocess

from PIL import Image

from cut_agent import graph, media, render
from cut_agent.config import ROOT
from cut_agent.runctl import new_run_id, read_json, write_json_atomic
from cut_agent.source_timing import FrameIndex


def make_source(folder: Path, mode: str, origin: int, rate: int, codec: str):
    name = f"{mode}_{origin}_{rate}.mkv"
    source = folder / name
    formula = "if(lt(N\\,5)\\,N\\,5+(N-5)*3)" if mode == "vfr" else "N"
    font = Path("C:/Windows/Fonts/msyh.ttc")
    label = (f",drawtext=fontfile={render._filter_value(font.as_posix())}:"
             "text='FRAME %{n}':fontsize=30:fontcolor=white:x=20:y=20") if font.is_file() else ""
    options = (["-pix_fmt", "yuv420p", "-x264-params", "scenecut=0:keyint=60:min-keyint=60", "-bf", "3"]
               if codec == "libx264" else [])
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
        f"color=black:size=640x480:rate={rate}:duration=1", "-vf",
        f"setpts={formula}+{origin}/TB,format=gbrp,"
        "geq=r='mod(N,4)*60':g='floor(N/4)*16':b=0,format=bgr0" + label,
        "-fps_mode", "vfr", "-c:v", codec, *options, str(source)], check=True)
    item = media._probe_video(source, folder)
    index = FrameIndex(item.source_timing)
    frames = folder.parent / "native" / name
    frames.mkdir(parents=True)
    subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-i", str(source),
        "-fps_mode", "passthrough", "-start_number", "0", str(frames / "frame_%03d.png")], check=True)
    pictures = sorted(frames.glob("*.png"))
    assert len(pictures) == index.count
    row = {**vars(item), "path": str(source),
           "shots": [{"idx": 1, "start": 0, "end": item.duration,
                      "frames": [str(p) for p in pictures],
                      "frame_times": [index.seconds(n) for n in range(index.count)]}]}
    return row, hashlib.sha256(source.read_bytes()).hexdigest()


def decoded_pixels(path):
    raw = subprocess.run([media._ffmpeg(), "-v", "error", "-i", str(path),
        "-vf", "format=rgb24,crop=1:1:640:360", "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return [list(raw[n:n + 3]) for n in range(0, len(raw), 3)]


def frame_number(pixel):
    number = round(pixel[0] / 60) + 4 * round(pixel[1] / 16)
    assert abs(pixel[0] - (number % 4) * 60) < 9
    assert abs(pixel[1] - (number // 4) * 16) < 9
    return number


def main():
    root = ROOT / "work" / "verification" / new_run_id()
    folder = root / "media"
    folder.mkdir(parents=True)
    definitions = [("cfr", 0, 10, "ffv1", [.11, .16], [(1, 5), (2, 6)]),
                   ("vfr", 2, 10, "ffv1", [.61, .81], [(5, 7), (6, 7)]),
                   ("cfr", 0, 60, "libx264", [.102], [(6, 30)])]
    rows, media_rows, hashes, expected = [], [], {}, []
    for mode, origin, rate, codec, starts, spans in definitions:
        item, digest = make_source(folder, mode, origin, rate, codec)
        media_rows.append(item)
        hashes[item["name"]] = digest
        for start, span in zip(starts, spans):
            rows.append({"seq": len(rows) + 1, "media": item["name"], "source": "local", "kind": "video",
                         "start_offset": start, "use_duration": .4, "segment_text": f"Source {item['name']} at {start}s"})
            expected.append(span)
    state = {"media_folder": str(folder), "copy": "source frame alignment", "media": media_rows,
             "timeline": rows, "_ctx": graph.RunCtx("frame-alignment", run_dir=root,
                                          options={"preview": True, "finishing_llm": False})}
    state.update(graph.write_doc(state))
    state.update(graph.finishing_guide(state))
    plan = read_json(root / "plan.json")
    pixels = decoded_pixels(root / "preview.mp4")
    assert plan["preview"]["status"] == "ready" and len(pixels) == plan["preview"]["frame_count"] == 53
    checked = []
    for row, card, marker, span, step in zip(plan["timeline"], plan["storyboard"]["cards"],
                                              plan["preview"]["markers"], expected, state["finishing"]["steps"]):
        assert not card["frame_missing"] and step["anchor"] == card
        assert (card["source_in_frame"], card["source_out_frame"]) == span
        assert row["source_alignment"]["requested_duration"] == .4
        assert row["start_offset"] == card["source_in_seconds"]
        assert abs(row["use_duration"] - (card["source_out_seconds"] - card["source_in_seconds"])) < 1e-9
        assert "实际起点" in card["source_timing_note"]
        assert marker["start"] == card["timeline_in_frame"] / 25
        assert marker["end"] == card["timeline_out_frame"] / 25
        sequence = [frame_number(p) for p in pixels[card["timeline_in_frame"]:card["timeline_out_frame"]]]
        assert sequence[0] == span[0] and sequence == sorted(sequence)
        assert all(span[0] <= number < span[1] for number in sequence)
        with Image.open(root / card["frame"]) as image:
            assert image.size == (640, 480)
        checked.append({"media": row["media"], "requested": row["source_alignment"], "frames": list(span),
                        "timeline_frames": [card["timeline_in_frame"], card["timeline_out_frame"]],
                        "preview_source_frames": sequence})
    for name, digest in hashes.items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
    for frame in (20, 43):
        subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-i", str(root / "preview.mp4"),
                        "-vf", f"select='eq(n,{frame})'", "-frames:v", "1",
                        str(root / f"preview_{frame:03d}.png")], check=True)
    report = {"result": "passed", "rows": checked, "preview_frames": len(pixels), "source_hashes": hashes,
              "scope": "真实 CFR/VFR/非零起点/H.264 B 帧；请求和源帧区间、执行卡、指导及全部预览帧对应；浏览器未验收"}
    write_json_atomic(root / "frame-alignment-verification.json", report)
    print(json.dumps({"result": "passed", "rows": len(checked), "frames": len(pixels),
                      "report": str(root / "frame-alignment-verification.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
