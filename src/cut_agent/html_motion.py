"""LLM-authored HTML motion graphics and deterministic browser-to-video rendering."""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from pathlib import Path

from . import llm
from .media import _ffmpeg
from .runctl import RunError
from .storyboard import frame_index

RESOLUTIONS = {"1080p": (1920, 1080), "4k": (3840, 2160)}
MAX_HTML_BYTES = 512_000

HTML_SYSTEM = """你是动态图形设计师和前端工程师。请为视频制作一段指定分辨率的 HTML 动画素材。
只能返回一个完整、独立的 HTML 文档（不要 Markdown 代码围栏）。允许 HTML/CSS/内联 SVG/JavaScript，禁止联网、
外部资源、音频、视频、iframe 和用户交互。所有字体使用系统 sans-serif。画面必须覆盖整个视口且没有滚动条。
渲染器会在每帧写入 window.__CUT_AGENT_TIME__（从 0 到 duration 的秒数）并派发 cutagentframe 事件；
动画必须监听该事件并完全由该时间值计算画面，不能依赖 requestAnimationFrame、Date.now 或随机数。
文案要清晰、留安全边距，最后一帧应能自然接入后一镜头。"""

PLAN_SYSTEM = """你是视频转场导演。根据整篇文案、相邻两个视频及用户要求，先判断这个过场应该如何呈现，
再决定需要几个连续画面。简单衔接只用 1 个画面；只有叙事确实需要时才拆成 2~6 个画面。每个画面之后会被写成
独立的动态 HTML 页面，因此必须描述可运动的视觉元素、运动路径、进入/退出方式，以及它如何承接上一个画面。
必须分析音乐速度、强弱、拍号和过场在音乐中的拍点；持续使用同一首音乐时要沿用当前拍点，单独过场音乐则从第一个强拍开始。
画面切换、文字出现和主要运动转折应落在拍点或小节强拍上。所有画面时长之和必须等于给定总时长。只输出 JSON：
{"concept":"整体设计与选择画面数量的理由","music_analysis":{"mode":"continuous|transition|none","bpm":数字或null,
"beats_per_bar":通常为4,"reason":"节奏判断"},"scenes":[{"duration":秒数,"beats":拍数或null,
"brief":"本画面的构图、文字、动态、拍点和衔接"}]}"""


def _extract_document(raw: str) -> str:
    fenced = re.search(r"```(?:html)?\s*(.*?)```", raw, re.I | re.S)
    html = (fenced.group(1) if fenced else raw).strip()
    start = re.search(r"<!doctype\s+html|<html\b", html, re.I)
    if start:
        html = html[start.start():]
    if not re.search(r"<html\b", html, re.I) or not re.search(r"</html\s*>", html, re.I):
        raise RunError("模型没有返回完整 HTML 文档")
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise RunError("生成的 HTML 超过 500 KiB 限制")
    forbidden = re.search(r"<(?:iframe|frame|object|embed|audio|video)\b", html, re.I)
    if forbidden:
        raise RunError(f"生成的 HTML 含禁用元素：{forbidden.group(0)}")
    if "cutagentframe" not in html or "__CUT_AGENT_TIME__" not in html:
        raise RunError("生成的 HTML 没有实现逐帧时间驱动动画")
    # Defense in depth: Chromium also aborts every non-local request while rendering.
    csp = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
           "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
           "font-src data:; connect-src 'none'; media-src 'none'; frame-src 'none'\">")
    if re.search(r"<head\b[^>]*>", html, re.I):
        html = re.sub(r"(<head\b[^>]*>)", r"\1" + csp, html, count=1, flags=re.I)
    else:
        html = re.sub(r"(<html\b[^>]*>)", r"\1<head>" + csp + "</head>", html, count=1, flags=re.I)
    return html


def generate(prompt: str, duration: float, before: dict | None, after: dict | None, *,
             copy: str = "", resolution: str = "4k") -> str:
    """Ask the configured LLM for one self-contained, offline animation document."""
    width, height = RESOLUTIONS.get(resolution, RESOLUTIONS["4k"])
    context = (
        f"输出画布：{width}×{height}（{resolution}）\n时长：{duration:.3f} 秒\n"
        f"整篇文案：{copy.strip() or '无'}\n"
        f"前一镜头：素材={(before or {}).get('media') or '无'}；旁白={(before or {}).get('segment_text') or '无'}\n"
        f"后一镜头：素材={(after or {}).get('media') or '无'}；旁白={(after or {}).get('segment_text') or '无'}\n"
        f"创作要求：{prompt.strip() or '根据相邻镜头设计简洁、连贯的过场动画'}"
    )
    return _extract_document(llm.chat(HTML_SYSTEM, context, temperature=.35, max_tokens=7000))


def _context(prompt: str, duration: float, before: dict, after: dict, copy: str,
             resolution: str, *, music: dict | None = None, music_mode: str = "auto",
             bpm: float | None = None, transition_start: float = 0) -> str:
    width, height = RESOLUTIONS[resolution]
    return (f"输出画布：{width}×{height}（{resolution}）\n总时长：{duration:.3f} 秒\n"
            f"整篇文案：{copy.strip() or '无'}\n"
            f"前一视频：素材={before.get('media') or '无'}；旁白={before.get('segment_text') or '无'}\n"
            f"后一视频：素材={after.get('media') or '无'}；旁白={after.get('segment_text') or '无'}\n"
            f"过场位于成片 {transition_start:.3f} 秒；音乐关系={music_mode}；用户指定 BPM={bpm or '未指定'}\n"
            f"当前音乐资料：{json.dumps(music or {}, ensure_ascii=False)[:4000]}\n"
            f"用户要求：{prompt.strip() or '由你根据上下文自主设计'}")


def plan_scenes(prompt: str, duration: float, before: dict, after: dict, *,
                copy: str, resolution: str, music: dict | None = None,
                music_mode: str = "auto", bpm: float | None = None,
                transition_start: float = 0) -> dict:
    """Let the LLM choose the transition concept and number of HTML pages."""
    raw = llm.chat_json(PLAN_SYSTEM, _context(prompt, duration, before, after, copy, resolution,
                                             music=music, music_mode=music_mode, bpm=bpm,
                                             transition_start=transition_start),
                        temperature=.25, max_tokens=2500)
    scenes = raw.get("scenes") if isinstance(raw, dict) else None
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 6:
        raise RunError("过场规划必须包含 1 到 6 个画面")
    cleaned = []
    for scene in scenes:
        if not isinstance(scene, dict) or not str(scene.get("brief", "")).strip():
            raise RunError("过场规划包含无效画面")
        try:
            seconds = float(scene.get("duration"))
        except (TypeError, ValueError):
            raise RunError("过场画面时长无效") from None
        if not math.isfinite(seconds) or seconds <= 0:
            raise RunError("过场画面时长必须为正数")
        beats = scene.get("beats")
        cleaned.append({"duration": seconds, "beats": beats if isinstance(beats, (int, float)) else None,
                        "brief": str(scene["brief"]).strip()})
    total = sum(scene["duration"] for scene in cleaned)
    # Keep the director's proportions while making the execution duration exact.
    for scene in cleaned:
        scene["duration"] = duration * scene["duration"] / total
    analysis = raw.get("music_analysis") if isinstance(raw.get("music_analysis"), dict) else {}
    effective_bpm = bpm if bpm is not None else analysis.get("bpm")
    try:
        effective_bpm = float(effective_bpm) if effective_bpm is not None else None
    except (TypeError, ValueError):
        effective_bpm = None
    if effective_bpm is not None and not 30 <= effective_bpm <= 300:
        effective_bpm = None
    effective_mode = music_mode if music_mode != "auto" else str(analysis.get("mode") or "continuous")
    if effective_mode == "none":
        effective_bpm = None
    beat_origin = transition_start if effective_mode == "transition" else 0.0
    if effective_bpm and len(cleaned) > 1:
        beat = 60 / effective_bpm
        desired, boundaries = 0.0, [0.0]
        for scene in cleaned[:-1]:
            desired += scene["duration"]
            absolute = transition_start + desired
            snapped = beat_origin + round((absolute - beat_origin) / beat) * beat
            relative = max(boundaries[-1] + min(.04, duration / 20), min(duration, snapped - transition_start))
            boundaries.append(relative)
        boundaries.append(duration)
        if all(a < b for a, b in zip(boundaries, boundaries[1:])):
            for index, scene in enumerate(cleaned):
                scene["duration"] = boundaries[index + 1] - boundaries[index]
                scene["start_beat"] = round((transition_start + boundaries[index] - beat_origin) / beat, 3)
    analysis.update(mode=effective_mode, bpm=effective_bpm,
                    beat_phase=round((transition_start - beat_origin) * effective_bpm / 60, 3) if effective_bpm else None)
    return {"concept": str(raw.get("concept", "")).strip(), "music_analysis": analysis, "scenes": cleaned}


def generate_pages(prompt: str, duration: float, before: dict, after: dict, *,
                   copy: str, resolution: str, music: dict | None = None,
                   music_mode: str = "auto", bpm: float | None = None,
                   transition_start: float = 0) -> tuple[dict, list[str]]:
    """Plan first, then author one animated HTML document for every planned scene."""
    plan = plan_scenes(prompt, duration, before, after, copy=copy, resolution=resolution, music=music,
                       music_mode=music_mode, bpm=bpm, transition_start=transition_start)
    base = _context(prompt, duration, before, after, copy, resolution, music=music,
                    music_mode=music_mode, bpm=bpm, transition_start=transition_start)
    pages = []
    count = len(plan["scenes"])
    for index, scene in enumerate(plan["scenes"], 1):
        user = (f"{base}\n整体方案：{plan['concept']}\n音乐节奏分析：{json.dumps(plan['music_analysis'], ensure_ascii=False)}\n"
                f"当前是第 {index}/{count} 个画面，"
                f"本页时长：{scene['duration']:.6f} 秒。\n本页导演说明：{scene['brief']}\n"
                "只实现当前画面；页面加载后的初始状态必须正确，并通过 cutagentframe 持续产生明显运动。")
        pages.append(_extract_document(llm.chat(HTML_SYSTEM, user, temperature=.35, max_tokens=7000)))
    return plan, pages


def save(root: Path, html: str) -> Path:
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
    target = root / "generated" / f"motion-{digest}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target.resolve()


def render(html_path: Path, out_path: Path, duration: float, fps: float, *,
           width: int = 3840, height: int = 2160) -> None:
    """Capture exact browser frames and encode them as a silent H.264 segment."""
    render_pages([{"path": str(html_path), "duration": duration}], out_path, duration, fps,
                 width=width, height=height)


def render_pages(pages: list[dict], out_path: Path, duration: float, fps: float, *,
                 width: int = 3840, height: int = 2160) -> None:
    """Render one or more animated HTML pages into one frame-continuous video."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RunError("HTML 动画渲染需要 Playwright") from exc
    frames = frame_index(duration, fps)
    frame_dir = out_path.parent / (out_path.stem + "_html_frames")
    shutil.rmtree(frame_dir, ignore_errors=True)
    frame_dir.mkdir(parents=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
            page = context.new_page()
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
            elapsed_frames = 0
            elapsed_seconds = 0.0
            for scene_index, scene in enumerate(pages):
                path = Path(str(scene["path"]))
                end_seconds = duration if scene_index == len(pages) - 1 else elapsed_seconds + float(scene["duration"])
                end_frame = frames if scene_index == len(pages) - 1 else frame_index(end_seconds, fps)
                page.goto(path.as_uri(), wait_until="load")
                for index in range(elapsed_frames, end_frame):
                    timestamp = (index - elapsed_frames) / fps
                    page.evaluate("""t => { window.__CUT_AGENT_TIME__ = t;
                        window.dispatchEvent(new CustomEvent('cutagentframe', {detail:{time:t}})); }""", timestamp)
                    page.screenshot(path=str(frame_dir / f"frame-{index:06d}.png"))
                elapsed_frames, elapsed_seconds = end_frame, end_seconds
            browser.close()
        command = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps),
                   "-i", str(frame_dir / "frame-%06d.png"), "-frames:v", str(frames),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", str(out_path)]
        import subprocess
        result = subprocess.run(command, capture_output=True, text=True, timeout=max(120, duration * 20))
        if result.returncode:
            raise RunError("HTML 动画编码失败：" + result.stderr[-400:])
    finally:
        shutil.rmtree(frame_dir, ignore_errors=True)
