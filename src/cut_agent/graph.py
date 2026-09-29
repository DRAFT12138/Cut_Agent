"""粗剪流水线（节点库）。

产出物是**粗剪方案文档**（时间线 + 镜头对齐 + 网络素材指引 + BGM 建议），
成片由人按方案手工精剪；流水线默认只出文档，可显式开启预览视频。

阶段顺序（P8 起由 runs.py 顺序驱动；节点保持纯函数 (state)->patch，
便于单独测试与断点续跑）：
  scan_media → plan_segments → understand_media → build_timeline → explore_web → pick_music → write_doc → finishing_guide
每个节点可经 state["_ctx"]（RunCtx）拿到控制标志/事件流/run 目录/seed；
无 _ctx 时（CLI 直调/测试）所有钩子为 no-op，行为与旧版一致。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .browser import BrowserController, BrowserError, controller
from .config import OUTPUT_DIR, SAMPLE_MEDIA, VISION_ENABLED, WORK_DIR
from .llm import LLMError, chat_json
from .media import MediaError, probe_media
from .musicfetch import MusicFetchError, download_for_mood
from .prompts import (DESCRIBE_MEDIA_SYSTEM, MATCH_SYSTEM, MUSIC_SYSTEM,
                      PLAN_SYSTEM, CutState)
from .runctl import RunControl, RunHalted, check_stop
from .checkpoints import UnitStore, fingerprint
from .shots import select_shot, shot_inventory, number
from .source_timing import align_timeline
from .craft import structure, critique
from .narrative import plan as plan_narrative
from .storyboard import build_storyboard, markdown as storyboard_markdown
from .mediacache import MediaCache
from .webfetch import acquire as acquire_web
from .finishing import build_guide, music_cues, markdown as finishing_markdown
from .vision import VisionError, analyse_with_llm
from .websearch import WebSearchError, client as ows_client

IMAGE_DEFAULT_SECONDS = 4.0   # 图片默认展示时长
VIDEO_DEFAULT_SECONDS = 6.0


@dataclass
class RunCtx:
    """阶段上下文（P8）：由 runs.py 驱动注入 state["_ctx"]。

    - control: 暂停/取消协作标志；check_stop 在单位边界调用；
    - log: 事件流（status/progress/log）；
    - run_dir: run 目录（阶段产物/帧/下载物的根）；
    - seed: LLM 随机种子（A/B 粗剪可复现），None=不设置。
    """
    run_id: str
    control: RunControl | None = None
    log: object | None = None
    run_dir: Path | None = None
    seed: int | None = None
    warnings: list[str] = field(default_factory=list)
    options: dict = field(default_factory=dict)


def _degraded(state, reason: str) -> None:
    ctx = _ctx(state)
    if ctx is not None and reason not in ctx.warnings:
        ctx.warnings.append(reason)


def _ctx(state: CutState) -> RunCtx | None:
    return state.get("_ctx")


def _seed_of(state: CutState) -> int | None:
    ctx = _ctx(state)
    return ctx.seed if ctx is not None else None


def _agent_decision(state: CutState, key: str):
    """Return a precomputed coding-agent decision, if this run supplied one."""
    ctx = _ctx(state)
    decisions = ctx.options.get("agent_decisions", {}) if ctx is not None else {}
    return decisions.get(key) if isinstance(decisions, dict) else None


def _checkpoint(state: CutState, stage: str) -> None:
    """单位边界检查点：暂停/取消生效。无控制（CLI 直调）时 no-op。"""
    ctx = _ctx(state)
    if ctx is not None and ctx.control is not None:
        check_stop(ctx.control, stage, ctx.log)


def _emit(state: CutState, stage: str, type_: str, **kw) -> None:
    ctx = _ctx(state)
    if ctx is not None and ctx.log is not None:
        ctx.log.event(stage, type_, **kw)


# ---------------- 节点 ----------------

def scan_media(state: CutState) -> dict:
    folder = Path(state["media_folder"])
    items = probe_media(folder, cache_dir=WORK_DIR / "media_cache")
    media = []
    ctx = _ctx(state)
    for i, it in enumerate(items, 1):
        thumbnails = list(it.thumbnails) if it.kind == "video" else [str(it.path)]
        if ctx and ctx.run_dir:
            # Own the already extracted images so later scans cannot replace a
            # finished run's thumbnails. No additional video frame extraction.
            from PIL import Image, ImageOps
            targets = []
            for j, source in enumerate(thumbnails, 1):
                target = ctx.run_dir / "frames" / f"scan_{i:04d}_{j}.png"
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with Image.open(source) as image:
                        image = ImageOps.exif_transpose(image).convert("RGB")
                        image.thumbnail((640, 360))
                        image.save(target)
                    targets.append(str(target.resolve()))
                except (OSError, ValueError):
                    _degraded(state, f"扫描缩略图不可用：{it.name}")
            thumbnails = targets
        media.append({
            "name": it.name, "kind": it.kind, "duration": it.duration,
            "width": it.width, "height": it.height,
            "fps": it.fps,
            "scene_cuts": [round(c, 1) for c in it.scene_cuts],
            "size_mb": round(it.size_mb, 1),
            "thumbnails": thumbnails,
            "source_timing": it.source_timing,
            "display_geometry": it.display_geometry,
        })
        if it.kind == "video" and it.source_timing.get("status") == "unavailable":
            _degraded(state, f"源帧索引不可用：{it.name}，{it.source_timing.get('error', '')}")
    return {"media": media,
            "log": state.get("log", []) + [f"扫描到 {len(media)} 个素材（视频 {sum(1 for m in media if m['kind']=='video')} / 图片 {sum(1 for m in media if m['kind']=='image')}）"]}


def _copy_duration_hint(copy: str) -> float:
    # 粗略估算朗读时长：中文 4.5 字/秒
    n = len(re.sub(r"\s", "", copy))
    return max(10.0, n / 4.5)


def understand_media(state: CutState) -> dict:
    """视觉理解：对每个视频做"变化驱动自适应抽帧 + 镜头描述"。

    - 抽帧不是均匀采样：画面变化小（静止镜头）→ 少抽；变化大（运镜/转场/动作）→ 多抽；
    - 镜头描述发给多模态 LLM，写入 media[i]["description"]，并补 scene_cuts（MAD 切点，
      比旧版 scene 滤镜阈值更稳，含镜头边界供 build_timeline 对齐 start_offset）；
    - LLM 不可用/失败时只保留几何信息（shots+切点），不阻塞主流程；
    - 视觉层整体关闭（CUT_AGENT_VISION=0）时直接跳过。
    """
    media = state.get("media", [])
    descriptions = _agent_decision(state, "media_descriptions")
    if isinstance(descriptions, dict):
        media = [{**row, "description": descriptions.get(row.get("name"), row.get("description", ""))}
                 for row in media]
    if _agent_decision(state, "segments") is not None:
        return {"media": media,
                "log": state.get("log", []) + ["编码代理已提供素材描述，跳过视觉模型 API"]}
    if not VISION_ENABLED:
        return {"media": media, "log": state.get("log", []) + ["视觉层已关闭（CUT_AGENT_VISION=0），跳过"]}
    media_folder = Path(state["media_folder"])
    ctx = _ctx(state)
    # 帧/候选缩略输出目录：run 目录（P8）；无 run（CLI 直调）时退回 work/vision 旧行为
    vwork = (ctx.run_dir / "frames") if (ctx is not None and ctx.run_dir is not None) else None
    n = len(media)
    units = UnitStore(ctx.run_dir if ctx else None, "understand_media")
    shared = MediaCache(WORK_DIR / "media_cache")
    enriched: list[dict] = []
    log_add: list[str] = []
    for i, m in enumerate(media, 1):
        _checkpoint(state, "understand_media")
        row = dict(m)
        if m["kind"] != "video":
            enriched.append(row)
            _emit(state, "understand_media", "progress", done=i, total=n, item=m["name"])
            continue
        _checkpoint(state, "understand_media")
        src = media_folder / m["name"]
        from . import vision, config
        key = {"version": 4, "path": str(src.resolve()),
               "media": {k: v for k, v in m.items() if k != "thumbnails"}, "file": fingerprint(src),
               "model": config.LLM_MODEL, "endpoint": config.LLM_BASE_URL,
               "vision": vision.geometry_settings()}
        saved = units.read(key)
        if saved is None:
            saved = shared.read(key, (vwork or WORK_DIR / "vision") / "cached")
            if saved:
                units.write(key, saved)
        if saved and all(Path(f).is_file() for shot in saved["row"].get("shots", [])
                         for f in shot.get("frames", [])):
            enriched.append({**saved["row"], "thumbnails": m.get("thumbnails", [])})
            log_add.extend(saved.get("log", []))
            for reason in saved.get("warnings", []):
                _degraded(state, reason)
            _emit(state, "understand_media", "progress", done=i, total=n,
                  item=m["name"], reused=True)
            continue
        if not src.exists():
            _degraded(state, f"素材不存在：{m['name']}")
            enriched.append(row)
            _emit(state, "understand_media", "progress", done=i, total=n, item=m["name"])
            continue
        _emit(state, "understand_media", "progress", done=i - 1, total=n,
              item=f"{m['name']} 分析中")
        log_start = len(log_add)
        warning_start = len(ctx.warnings) if ctx else 0
        try:
            vv = analyse_with_llm(src, float(m.get("duration", 0.0)), work_dir=vwork)
        except (VisionError, MediaError) as e:
            _degraded(state, f"视觉分析失败：{m['name']}，{e}")
            log_add.append(f"视觉分析失败（降级为元数据）{m['name']}: {e}")
            enriched.append(row)
        else:
            row["scene_cuts"] = vv.scene_cuts or row.get("scene_cuts", [])
            row["shots"] = [s.to_dict() for s in vv.shots]
            row["geometry_reused"] = getattr(vv, "geometry_reused", False)
            row["sampling"] = getattr(vv, "sampling", {})
            row["n_frames"] = len(vv.frames)
            row["description"] = vv.description
            if any(not s.frames for s in vv.shots):
                _degraded(state, f"部分镜头缺少代表帧：{m['name']}，需要重新抽帧或人工检查")
            if any(not s.description for s in vv.shots):
                _degraded(state, f"部分镜头缺少描述：{m['name']}，回退文件摘要")
            if not vv.description:
                _degraded(state, f"未获得视觉描述：{m['name']}，仅保留几何信息")
            enriched.append(row)
            desc_short = (vv.description[:48] + "…") if len(vv.description) > 48 else (vv.description or "（LLM 未返回描述）")
            reused = "（复用几何抽帧）" if row["geometry_reused"] else ""
            log_add.append(f"视觉 {m['name']}: {len(vv.shots)} 镜头 / 抽 {len(vv.frames)} 帧{reused} — {desc_short}")
            sampling = row["sampling"]
            if sampling.get("effective_budget", 0) > sampling.get("target_budget", 0):
                log_add.append(f"为覆盖全部镜头，{m['name']} 帧预算从 {sampling['target_budget']} 调整至 {sampling['effective_budget']}")
        record = {"row": row, "log": log_add[log_start:],
                  "warnings": ctx.warnings[warning_start:] if ctx else []}
        units.write(key, record)
        shared.write(key, record)
        _emit(state, "understand_media", "progress", done=i, total=n, item=m["name"])
    return {"media": enriched,
            "log": state.get("log", []) + log_add}


def plan_segments(state: CutState) -> dict:
    copy = state["copy"]
    est = _copy_duration_hint(copy)
    user = f"预计朗读时长约 {est:.0f} 秒。文案如下：\n\n{copy}"
    _checkpoint(state, "plan_segments")
    data = _agent_decision(state, "segments")
    _emit(state, "plan_segments", "progress", done=0, total=1,
          item="编码代理分段" if data is not None else "LLM 切段")
    if data is None:
        try:
            data = chat_json(PLAN_SYSTEM, user, max_tokens=4000, seed=_seed_of(state))
        except LLMError as e:
            data = None
            state.get("log", []).append(f"plan_segments LLM 失败，用规则切段降级: {e}")
    segs = []
    if isinstance(data, list):
        segs = data
    elif isinstance(data, dict):
        segs = data.get("segments", [])
    norm = []
    for i, s in enumerate(segs, 1):
        if not isinstance(s, dict) or not str(s.get("text", "")).strip():
            continue
        s.setdefault("mood", "")
        try:
            s["duration"] = float(s.get("duration", 5))
        except (TypeError, ValueError):
            s["duration"] = VIDEO_DEFAULT_SECONDS
        s["kw_cn"] = s.get("kw_cn") or []
        s["kw_en"] = s.get("kw_en") or []
        norm.append(s)
    if not norm:
        _degraded(state, "模型未返回有效分段，采用规则切段")
        # 规则降级：按句子切段，时长按 4.5 字/秒估算
        import textwrap
        parts = [p.strip() for p in re.split(r"[。！？!?\n；;]", copy) if p.strip()]
        parts = textwrap.wrap("".join(parts), width=40) or [copy.strip()]
        for p in parts[:10]:
            norm.append({"text": p, "mood": "", "duration": round(max(2.0, len(p) / 4.5), 1),
                         "kw_cn": [p[:8]], "kw_en": ["b-roll"]})
    norm, narrative = plan_narrative(copy, norm)
    norm = structure(norm)
    return {"segments": norm, "narrative": narrative,
            "log": state.get("log", []) + [f"文案切成 {len(norm)} 段"]}


def build_timeline(state: CutState) -> dict:
    media = state["media"]
    segs = state["segments"]
    if not media:
        timeline = [{"seq": i, "media": "", "source": "web", "kind": "video",
                     "needs_web": True, "start_offset": 0, "use_duration": number(s.get("duration"), 6),
                     "segment_text": s.get("text", ""), "role": s.get("role", "发展"),
                     "intensity": s.get("intensity", 3), "web_query": _query_for({"segment_text": s.get("text", "")}, segs)}
                    for i, s in enumerate(segs, 1)]
        timeline, report = critique(timeline, [], auto_fix=False)
        return {"timeline": timeline, "critique": report,
                "log": state.get("log", []) + ["没有本地素材，全部转网络素材"]}
    media_lines = shot_inventory(media)
    seg_lines = []
    for i, s in enumerate(segs, 1):
        seg_lines.append(f"{i}. [{s.get('narrative_role', s.get('role', ''))}] {s.get('text','')}（视觉目标: {s.get('visual_goal','')}；必须实体: {', '.join(s.get('required_entities', [])) or '无'}；情绪: {s.get('emotion', s.get('mood',''))}；约{s.get('duration',0)}s；关键词: {', '.join(s.get('kw_en',[])) or ', '.join(s.get('kw_cn',[]))}）")
    narrative = state.get("narrative", {})
    chapter_lines = [f"{chapter['title']}({chapter['purpose']}): {', '.join(chapter['segment_ids'])}"
                     for chapter in narrative.get("chapters", [])]
    user = ("全篇结构（镜头选择不得破坏钩子、推进、转折和结尾回收）：\n" + "\n".join(chapter_lines)
            + "\n\n本地素材清单：\n" + "\n".join(media_lines) + "\n\n文案分段：\n" + "\n".join(seg_lines))
    _checkpoint(state, "build_timeline")
    data = _agent_decision(state, "timeline")
    _emit(state, "build_timeline", "progress", done=0, total=1,
          item="编码代理编排" if data is not None else "LLM 编排")
    if data is None:
        try:
            data = chat_json(MATCH_SYSTEM, user, max_tokens=6000, seed=_seed_of(state))
        except LLMError as e:
            data = None
            state.get("log", []).append(f"build_timeline LLM 失败，用轮播规则降级: {e}")
    tl = []
    if isinstance(data, list):
        tl = data
    elif isinstance(data, dict):
        tl = data.get("timeline", [])
    if not tl:
        _degraded(state, "模型未返回时间线，采用本地素材轮播")
        # 规则降级：本地素材轮播（视频优先），轮完的段转网络
        videos = [m for m in media if m["kind"] == "video"]
        images = [m for m in media if m["kind"] == "image"]
        pool = videos or media
        for i, s in enumerate(segs, 1):
            if pool and i <= len(pool) * 2:
                m = pool[(i - 1) % len(pool)]
                kind = m["kind"]
                dur = float(s.get("duration", VIDEO_DEFAULT_SECONDS))
                if kind == "video":
                    dur = min(dur, max(1.0, m["duration"] - 0.2))
                    row = {"media": m["name"], "kind": "video", "use_duration": round(dur, 1),
                           "start_offset": 0, "needs_web": False}
                else:
                    row = {"media": m["name"], "kind": "image", "use_duration": round(dur, 1),
                           "start_offset": 0, "needs_web": False}
            else:
                row = {"media": "", "kind": "video", "use_duration": VIDEO_DEFAULT_SECONDS,
                       "start_offset": 0, "needs_web": True,
                       "web_query": ", ".join(s.get("kw_en", [])) or ", ".join(s.get("kw_cn", []))}
            row["segment_text"] = s.get("text", "")
            row["source"] = "local" if row["media"] else "web"
            tl.append(row)
    names = {m["name"] for m in media}
    cleaned = []
    for i, row in enumerate(tl, 1):
        if not isinstance(row, dict):
            continue
        name = row.get("media") or ""
        if name not in names:
            # 模型可能给了近似名，找前缀匹配
            cand = sorted(n for n in names if name and (n.split(".")[0] in name or name.split(".")[0] in n))
            name = cand[0] if cand else ""
        row["media"] = name
        row["seq"] = i
        row.setdefault("segment_text", segs[i - 1].get("text", "") if i <= len(segs) else "")
        row["needs_web"] = bool(row.get("needs_web")) or not name
        row.setdefault("kind", "video")
        row.setdefault("use_duration", VIDEO_DEFAULT_SECONDS)
        try:
            row["use_duration"] = float(row["use_duration"])
        except (TypeError, ValueError):
            row["use_duration"] = VIDEO_DEFAULT_SECONDS
        if name:
            row = select_shot(row, next(m for m in media if m["name"] == name))
        else:
            row.update(source="web", start_offset=0, shot_idx=None, span=False,
                       use_duration=max(.04, number(row.get("use_duration"), 6.0)))
        segment = next((s for s in segs if s.get("text") == row.get("segment_text")),
                       segs[i - 1] if i <= len(segs) else {})
        row["role"] = segment.get("role", "发展")
        row["intensity"] = segment.get("intensity", 3)
        cleaned.append(row)
    cleaned, report = critique(cleaned, media)
    return {"timeline": cleaned, "critique": report,
            "log": state.get("log", []) + [f"生成时间线 {len(cleaned)} 行"]}


def _query_for(t: dict, segments: list[dict]) -> str:
    q = (t.get("web_query") or "").strip()
    if not q:
        seg = next((s for s in segments if s.get("text") == t.get("segment_text")), {})
        q = ", ".join(seg.get("kw_en", [])) or ", ".join(seg.get("kw_cn", [])) or "b-roll"
    return q


def _explore_via_owsearch(t: dict, q: str, web_assets: list[dict]) -> None:
    """首选路径：open-webSearch 守护进程搜索 → 取结果页里的直链媒体。

    比直接爬 Bing 图片页更稳（多引擎、结构化）。守护进程不可用时抛异常，由上层降级。
    """
    c = ows_client()
    if not c.health():
        raise WebSearchError("open-webSearch 守护进程未启动（node tools/open-webSearch/build/index.js serve）")
    engines = ["bing"] if not any("\u4e00" <= ch <= "\u9fff" for ch in q) else ["baidu", "bing"]
    resp = c.search(q, limit=5, engines=engines)
    # 1) 结果里本身就是媒体直链
    for h in resp.results:
        if re.search(r"\.(mp4|mov|webm|jpg|jpeg|png|webp)$", h.url, re.I):
            web_assets.append({"name": f"web_{len(web_assets)+1}", "url": h.url,
                               "query": q, "for_segment": t.get("segment_text", ""),
                               "kind": "video" if re.search(r"\.(mp4|mov|webm)$", h.url, re.I) else "image",
                               "from": f"{h.engine}:{h.title}"[:80]})
    # 2) 抓 2~3 个结果页，找页内图片/视频直链（免版权站优先）
    page_budget = 3
    for h in resp.results:
        if page_budget <= 0:
            break
        try:
            d = c.fetch_web(h.url, max_chars=20000, render_mode="auto")
            page = d.get("content") or ""
            found = re.findall(r'https?://[^\s"\'<>)]+\.(?:mp4|webm|jpg|jpeg|png|webp)(?:\?[^\s"\'<>)]*)?',
                               page, re.I)
            # 过滤明显无关的（头像/logo/icon）
            found = [u for u in found if not re.search(r"(avatar|logo|icon|emoji|favicon)", u, re.I)]
            seen = {a["url"] for a in web_assets}
            added = 0
            for u in found:
                if u in seen or added >= 2:
                    continue
                seen.add(u)
                kind = "video" if re.search(r"\.(mp4|webm)", u, re.I) else "image"
                web_assets.append({"name": f"web_{len(web_assets)+1}", "url": u,
                                   "query": q, "for_segment": t.get("segment_text", ""),
                                   "kind": kind, "from": f"{h.title}"[:80]})
                added += 1
            page_budget -= 1
            if any(a.get("from") == f"{h.title}"[:80] for a in web_assets):
                break  # 该查询已有素材，换下一个 needs
        except WebSearchError:
            continue
        except Exception:
            continue


def _explore_via_browser(bc, t: dict, q: str, web_assets: list[dict]) -> None:
    """回退路径：原 Playwright Bing 图片搜索。"""
    imgs = bc.find_images(q, count=2)
    for im in imgs:
        web_assets.append({"name": f"web_{len(web_assets)+1}", "url": im["url"],
                           "query": q, "for_segment": t.get("segment_text", ""),
                           "kind": "image", "from": "bing-images"})


def explore_web(state: CutState) -> dict:
    """联网找素材：首选 open-webSearch 守护进程，失败降级 Playwright。不阻塞主流程。"""
    needs = [t for t in state.get("timeline", []) if t.get("needs_web") or t.get("source") == "web"]
    web_assets: list[dict] = []
    if not needs:
        return {"web_assets": [], "log": state.get("log", []) + ["本地素材充足，跳过联网探索"]}

    ctx = _ctx(state)
    units = UnitStore(ctx.run_dir if ctx else None, "explore_web")
    _checkpoint(state, "explore_web")
    ows_ok = None
    bc = None
    try:
        for idx, t in enumerate(needs, 1):
            _checkpoint(state, "explore_web")
            q = _query_for(t, state.get("segments", []))
            key = {"index": idx, "row": t, "query": q}
            saved = units.read(key)
            if saved is not None:
                web_assets.extend({**asset, "for_seq": asset.get("for_seq", t.get("seq"))}
                                  for asset in saved["assets"])
                for asset in saved["assets"]:
                    if asset.get("error"):
                        _degraded(state, f"搜索失败：{q}，{asset['error']}")
                _emit(state, "explore_web", "progress", done=idx, total=len(needs), item=q, reused=True)
                continue
            if ows_ok is None:
                try:
                    ows_ok = ows_client().health()
                except Exception:
                    ows_ok = False
                if not ows_ok:
                    try:
                        bc = controller().start()
                    except Exception:
                        pass
            before = len(web_assets)
            _emit(state, "explore_web", "progress", done=idx - 1, total=len(needs), item=q)
            try:
                if ows_ok:
                    _explore_via_owsearch(t, q, web_assets)
                elif bc is not None:
                    _explore_via_browser(bc, t, q, web_assets)
                else:
                    raise RuntimeError("open-webSearch 守护进程与浏览器均不可用")
            except RunHalted:
                raise
            except Exception as e:
                _degraded(state, f"搜索失败：{q}，{e}")
                web_assets.append({"name": f"web_{len(web_assets)+1}", "url": "",
                                   "query": q, "for_segment": t.get("segment_text", ""),
                                   "kind": "image", "error": str(e)})
            for asset in web_assets[before:]:
                asset["for_seq"] = t.get("seq")
            units.write(key, {"assets": web_assets[before:]})
            _emit(state, "explore_web", "progress", done=idx, total=len(needs), item=q)
            if bc is not None:
                try:
                    bc.screenshot(f"explore_{int(time.time())}")
                except Exception:
                    pass
        if bc is not None:
            bc.stop()
    except RunHalted:
        if bc is not None:
            try:
                bc.stop()
            except Exception:
                pass
        raise
    except Exception as e:
        if bc is not None:
            try:
                bc.stop()
            except Exception:
                pass
        return {"web_assets": web_assets,
                "log": state.get("log", []) + [f"联网探索失败（降级继续）: {e}"]}
    via = "open-webSearch" if ows_ok else "playwright"
    verified = []
    destination = (ctx.run_dir if ctx and ctx.run_dir else WORK_DIR) / "web"
    for index, asset in enumerate(web_assets):
        _checkpoint(state, "explore_web")
        checked = acquire_web(asset, destination, WORK_DIR / "web_cache")
        verified.append(checked)
        if checked["status"] != "verified":
            _degraded(state, f"网络素材待人工：{checked.get('error', '验证失败')}")
        _emit(state, "explore_web", "progress", done=index + 1, total=len(web_assets), item="验证 " + asset.get("name", ""))
    from copy import deepcopy
    timeline = deepcopy(state.get("timeline", []))
    for row in timeline:
        if not (row.get("needs_web") or row.get("source") == "web"):
            continue
        seq = row.get("seq")
        asset = next((a for a in verified if a["status"] == "verified"
                      and seq is not None and a.get("for_seq") == seq), None)
        if asset is None:
            # Older stage/unit files did not record a row number. Only use
            # their text fallback; never borrow another numbered row's asset.
            asset = next((a for a in verified if a["status"] == "verified"
                          and a.get("for_seq") is None
                          and a.get("for_segment") == row.get("segment_text")), None)
        if asset:
            row.update(source="web", needs_web=False, local_path=asset["local"], ref=asset["local"],
                       media=asset.get("name") or Path(asset["local"]).name,
                       asset_status="verified", thumbnail=asset.get("thumbnail"), kind=asset["kind"],
                       source_url=asset["url"], start_offset=0, source_fps=asset["fps"],
                       source_timing=asset.get("source_timing", {}))
            if asset["kind"] == "video":
                row["use_duration"] = min(number(row.get("use_duration"), 6), asset["duration"])
        else:
            row["asset_status"] = "manual"
    timeline = align_timeline(timeline, state.get("media", []))
    timeline, report = critique(timeline, state.get("media", []), auto_fix=False)
    report["repairs"] = state.get("critique", {}).get("repairs", [])
    report["repair_passes"] = state.get("critique", {}).get("repair_passes", 0)
    return {"web_assets": verified, "timeline": timeline, "critique": report,
            "log": state.get("log", []) + [f"联网探索（{via}）得到 {len(web_assets)} 个网络素材"]}


def pick_music(state: CutState) -> dict:
    """推荐配乐 + 自动下载一首免版权 BGM。

    下载路径（按顺序降级）：
    1) freepd.cn 直链（CC0/公共领域，已实测可用；原 freepd.com 已于 2026-01 关站）
    2) Playwright 浏览器搜索 mp3 直链（旧路径，保留兼容）
    3) 均失败 → 在文档中给出人工检索指引
    """
    segs = state.get("segments", [])
    moods = "、".join(dict.fromkeys(s.get("mood", "") for s in segs if s.get("mood")))
    copy_head = state.get("copy", "")[:120]
    _checkpoint(state, "pick_music")
    rec = _agent_decision(state, "music")
    if rec is None:
        try:
            rec = chat_json(MUSIC_SYSTEM, f"文案开头：{copy_head}\n整体情绪：{moods or '通用'}",
                            max_tokens=1200, seed=_seed_of(state))
            if not isinstance(rec, dict):
                rec = {}
        except LLMError as e:
            # 单 LLM 实例被争用时可能失败：用分段情绪做规则推荐，不挡下载
            _degraded(state, f"配乐推荐失败，采用情绪规则：{e}")
            rec = {"mood": moods or "通用",
                   "primary": {"title": f"（推荐服务暂不可用，按情绪「{moods or '通用'}」到 freepd.cn 选曲）",
                               "artist": "FreePD", "reason": f"LLM 推荐失败: {e}"},
                   "alternatives": []}
    rec.setdefault("mood", moods or "通用")
    rec.setdefault("alternatives", [])
    music = {"primary": rec.get("primary", {}) if isinstance(rec.get("primary"), dict) else {},
             "alternatives": rec.get("alternatives", []) if isinstance(rec.get("alternatives"), list) else [],
             "mood": rec["mood"], "downloads": [], "source": ""}
    mood_q = rec["mood"] or "cinematic"
    _emit(state, "pick_music", "progress", done=0, total=1, item=f"下载 {mood_q}")

    # 1) freepd.cn 按情绪分类直链下载
    try:
        _checkpoint(state, "pick_music")
        got = download_for_mood(mood_q, WORK_DIR / "music", count=1, max_tries=4)
        if got:
            music["downloads"].extend(got)
            music["source"] = "freepd.cn (CC0)"
    except RunHalted:
        raise
    except Exception as e:
        music.setdefault("download_error", f"freepd.cn 下载失败: {e}")

    # 2) 回退：浏览器搜索
    if not music["downloads"]:
        try:
            bc = controller().start()
            hits = bc.find_music(mood_q, count=3)
            for h in hits:
                url = h.url or h.page_url
                if not url:
                    continue
                dest = WORK_DIR / "music" / (re.sub(r"[^\w\-]", "_", h.title or "track")[:40] + ".mp3")
                try:
                    path = bc.download(url, dest, referer=h.page_url)
                    if path.stat().st_size > 20_000:
                        music["downloads"].append({"title": h.title, "url": url,
                                                   "local": str(path),
                                                   "size_kb": path.stat().st_size // 1024})
                        music["source"] = "browser-search"
                        break
                except Exception:
                    continue
            bc.stop()
        except Exception as e:
            music["download_error"] = f"freepd.cn 与浏览器下载均失败: {e}"

    if not music.get("downloads"):
        _degraded(state, "未下载到配乐，需要人工检索")
        music["download_error"] = ("未自动下载到 BGM：" + music.get("download_error", "") +
                                   " 请按推荐关键词到 freepd.cn / pixabay.com 检索，"
                                   "或提供 Pexels/Pixabay API key 后自动抓取。")
    return {"music": music,
            "log": state.get("log", []) + [f"配乐：主 BGM {music['primary'].get('title', '待定')}" +
                                           (f"（已下载 {music['downloads'][0]['local']}，{music['source']}）" if music["downloads"] else "")]}


# ---------------- 文档 ----------------

def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m, s = divmod(int(round(seconds)), 60)
    return f"{m:02d}:{s:02d}"


def _line_field(value) -> str:
    """A physical TSV row must never be split by narration or notes."""
    return re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", str(value or ""))


def _render_lines(timeline, cards) -> list[str]:
    lines = []
    for t, card in zip(timeline, cards, strict=True):
        name = t.get("media") or ""
        kind = t.get("kind", "video")
        dur = float(t.get("use_duration", 0))
        off = float(t.get("start_offset", 0)) if kind == "video" else 0.0
        src = t.get("source", "local")
        tag = "本地" if src == "local" else "网络"
        start = f"起点{fmt_time(off)}（{off:.1f}s）" if kind == "video" else "-"
        if src == "web":
            status = "已验证" if t.get("asset_status") == "verified" else "待人工替换"
        else:
            status = "本地素材"
        fields = [name or "待补", "视频" if kind == "video" else "图片", fmt_time(dur), start,
                  tag, t.get("segment_text", ""), t.get("note", ""),
                  t.get("shot_idx") or "-", card["source_in_tc"], card["source_out_tc"],
                  card["timeline_in_tc"], card["timeline_out_tc"], status,
                  card.get("source_timing_note", "")]
        lines.append("\t".join(_line_field(value) for value in fields))
    return lines


def write_doc(state: CutState) -> dict:
    state = {**state, "timeline": align_timeline(state.get("timeline") or [], state.get("media") or [])}
    timeline = state.get("timeline") or []
    web_assets = state.get("web_assets") or []
    music = state.get("music") or {}
    ctx = _ctx(state)
    # 文档输出目录：run 目录（P8，文档随 run 走）；无 run 时退回全局 output/（旧行为）
    doc_dir = (ctx.run_dir if (ctx is not None and ctx.run_dir is not None) else OUTPUT_DIR)
    doc_dir.mkdir(parents=True, exist_ok=True)
    storyboard = build_storyboard(state, doc_dir)
    lines = _render_lines(timeline, storyboard["cards"])
    cues = music_cues(timeline, storyboard)
    preview = {"status": "stale"} if state.get("preview") else None
    if ctx and ctx.options.get("preview"):
        from .render import render_video
        downloads = music.get("downloads", [])
        music_path = Path(downloads[0]["local"]) if downloads and downloads[0].get("local") else None
        try:
            preview = render_video(timeline, Path(state["media_folder"]),
                                   {m["name"]: m for m in state.get("media", [])}, music_path,
                                   doc_dir / "preview.mp4", fps=storyboard["timeline_fps"],
                                   progress=lambda done, total, item: _emit(state, "write_doc", "progress", done=done, total=total, item=item))
            preview["status"] = "ready"
        except RunHalted:
            raise
        except Exception as exc:
            preview = {"status": "failed", "error": str(exc)[:300]}
            _degraded(state, "预览失败，文档正常生成：" + str(exc)[:180])

    # 1) 机器可读行文档（每行一个素材）
    line_doc = doc_dir / "cut_lines.txt"
    header = ("文件\t类型\t持续时间\t视频起点\t来源\t对应旁白\t备注\t镜头号\t"
              "源入点\t源出点（不含）\t成片入点\t成片出点（不含）\t素材状态\t源帧定位依据\n")
    line_doc.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")

    # 2) 人类可读粗剪文档
    doc = doc_dir / "粗剪方案.md"
    md = ["# 视频粗剪方案\n"]
    md.append("[精剪指导](精剪指导.md)\n")
    if preview and preview.get("status") == "ready":
        md.append("[播放粗剪预览](preview.mp4)\n")
    elif preview:
        md.append("- 预览暂不可用，请根据当前计划重新生成。\n")
    md.append(f"- 素材目录：`{state['media_folder']}`")
    md.append(f"- 本地素材 {len(state.get('media', []))} 个；时间线 {len(timeline)} 行")
    total = sum(float(t.get('use_duration', 0)) for t in timeline)
    md.append(f"- 成片预估时长：{fmt_time(total)}")
    md.append("- 本文档即最终产出：请按下表逐行取材拼接（精剪阶段再做转场/调色/节奏）\n")
    md.append("## 文案\n\n> " + state.get("copy", "").replace("\n", "\n> ") + "\n")
    md.extend(storyboard_markdown(storyboard))
    md.append("\n## 时间线明细\n")
    md.append("| # | 文件 | 类型 | 持续时间 | 视频起点 | 来源 | 对应旁白 | 备注 |")
    md.append("|---|------|------|----------|----------|------|----------|------|")
    for i, t in enumerate(timeline, 1):
        kind = t.get("kind", "video")
        dur = float(t.get("use_duration", 0))
        off = float(t.get("start_offset", 0)) if kind == "video" else 0
        off_s = f"{off:.1f}s" if kind == "video" else "-"
        src = "本地" if t.get("source", "local") == "local" else "网络"
        cells = [i, t.get("media") or "待补", kind, fmt_time(dur), off_s, src,
                 t.get("segment_text", ""), t.get("note", "")]
        def table_cell(value):
            return " ".join(str(value or "").splitlines()).replace("\\", "\\\\").replace("|", "\\|")
        md.append("| " + " | ".join(table_cell(cell) for cell in cells) + " |")
    md.append("\n## 网络素材（浏览器探索得到）\n")
    if web_assets:
        for w in web_assets:
            err = f"（下载失败: {w.get('error','')}）" if w.get("error") else ""
            status = "已验证入库" if w.get("status") == "verified" else "待人工"
            md.append(f"- [{status}] `{w['name']}` → {w.get('url') or '无直链'} （搜索: {w.get('query')}{err}）")
            if w.get("local"):
                export_root = Path(state.get("_export_root", doc_dir))
                relative = Path(w["local"]).relative_to(export_root).as_posix() if Path(w["local"]).is_relative_to(export_root) else w["local"]
                md.append(f"  - 本地素材：[打开](<{relative}>)；{w.get('width')}×{w.get('height')}")
    else:
        md.append("- 无（本地素材已覆盖）")
    md.append("\n## 配乐\n")
    md.append(f"- 情绪：{music.get('mood','')}\n")
    for cue in cues:
        md.append(f"- 高潮锚点：{cue['timecode']}（帧 {cue['frame']}）：{cue['instruction']}")
    p = music.get("primary", {})
    md.append(f"- **主 BGM**：{p.get('title','')} {('- ' + p.get('artist','')) if p.get('artist') else ''} — {p.get('reason','')}")
    for a in music.get("alternatives", []):
        md.append(f"- 备选：{a.get('title','')} {('- ' + a.get('artist','')) if a.get('artist') else ''} — {a.get('reason','')}")
    if music.get("downloads"):
        for d in music["downloads"]:
            lic = f"，{d['license']}" if d.get("license") else ""
            md.append(f"- 已下载：`{d['local']}`（{d['size_kb']} KB{lic}，来源 {d['url']}）")
    elif music.get("download_error"):
        md.append(f"- 下载失败：{music['download_error']}（请手动按上面关键词到免版权站获取）")
    report = state.get("critique") or {}
    narrative = state.get("narrative") or {}
    md.append("\n## 叙事结构\n")
    for chapter in narrative.get("chapters", []):
        md.append(f"- {chapter['title']}（{chapter['purpose']}）：" + "、".join(chapter["segment_ids"]))
    for suggestion in narrative.get("suggestions", []):
        md.append(f"- 建议 {suggestion['type']}：{'、'.join(suggestion['segment_ids'])} — {suggestion['reason']}")
    md.append("\n## 自检报告\n")
    md.append("- 结构弧线：" + " → ".join(str(x) for x in report.get("intensity_arc", [])))
    md.append("- " + report.get("disclosure", "尚未执行自检"))
    for repair in report.get("repairs", []):
        md.append("- 已修复：" + repair["message"])
    for warning in report.get("warnings", []):
        label = f"第 {warning['seq']} 行：" if warning.get("seq") else ""
        md.append("- ⚠ " + label + warning["message"])
    if report and not report.get("warnings"):
        md.append("- 本轮规则未发现待处理告警；仍需人工审核语义。")
    md.append("\n## 运行日志\n")
    for l in state.get("log", []):
        md.append(f"- {l}")
    doc.write_text("\n".join(md) + "\n", encoding="utf-8")
    (doc_dir / "plan.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # Phase 0 export snapshot. Editing/rebuild becomes authoritative in P4.
    from .runctl import write_json_atomic
    plan_path = doc.parent / "plan.json"
    write_json_atomic(plan_path, {
        "schema_version": 1,
        "storyboard": storyboard,
        "preview": preview,
        "music_cues": cues,
        "platform": state.get("platform") or (ctx.options.get("platform", "douyin") if ctx else "douyin"),
        **{key: state.get(key) for key in
           ("media_folder", "copy", "media", "segments", "narrative", "timeline", "web_assets", "music", "critique")},
        "segments": state.get("segments") or [], "timeline": timeline,
        "web_assets": web_assets, "music": music, "critique": report,
        "doc_path": str(doc), "line_doc_path": str(line_doc),
    })

    return {"doc_path": str(doc), "line_doc_path": str(line_doc),
            "timeline": timeline,
            "segments": state.get("segments") or [], "narrative": narrative,
            "web_assets": web_assets, "music": music,
            "critique": report,
            "storyboard": storyboard,
            "preview": preview,
            "music_cues": cues,
            "plan_path": str(plan_path),
            "log": state.get("log", []) + [f"文档已写出: {doc.name} / {line_doc.name}"]}


def finishing_guide(state: CutState) -> dict:
    """Stage 8: deterministic guide with optional LLM color/title annotations."""
    _checkpoint(state, "finishing_guide")
    ctx = _ctx(state)
    options = ctx.options if ctx else {}
    editorial = {}
    if options.get("finishing_llm", True):
        import json
        prompt = ('你是精剪指导。只给调色和标题建议，不计算时间码。输出 JSON：'
                  '{"rows":[{"seq":1,"color":"具体调色建议"}],"titles":["标题建议"]}。'
                  '依据已提供镜头描述，不虚构画面细节。')
        try:
            editorial = chat_json(prompt, json.dumps({"copy": state.get("copy", ""),
                                   "rows": [{"seq": r.get("seq"), "description": r.get("shot_description", ""),
                                             "text": r.get("segment_text", "")} for r in state.get("timeline", [])]},
                                   ensure_ascii=False), max_tokens=2200, seed=_seed_of(state))
            if not isinstance(editorial, dict) or not isinstance(editorial.get("rows"), list):
                _degraded(state, "精剪文字建议未返回有效结构，采用离线规则")
        except LLMError as exc:
            _degraded(state, f"精剪文字建议采用离线规则：{exc}")
    guide = build_guide(state, state.get("platform") or options.get("platform", "douyin"), editorial)
    root = ctx.run_dir if ctx and ctx.run_dir else OUTPUT_DIR
    root.mkdir(parents=True, exist_ok=True)
    from PIL import Image, ImageOps
    for i, cover in enumerate(guide["covers"], 1):
        target = root / "frames" / f"cover_{i:02d}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(cover["frame"]) as image:
            ImageOps.exif_transpose(image).convert("RGB").save(target)
        cover["frame"] = target.relative_to(root).as_posix()
    path = root / "精剪指导.md"
    path.write_text(finishing_markdown(guide), encoding="utf-8")
    from .runctl import read_json, write_json_atomic
    plan = read_json(root / "plan.json")
    if isinstance(plan, dict):
        plan.update(finishing=guide, guide_path=str(path))
        write_json_atomic(root / "plan.json", plan)
    return {"finishing": guide, "guide_path": str(path),
            "log": state.get("log", []) + [f"精剪指导已写出：{path.name}"]}


# ---------------- 顺序执行器（P8 起不再用 LangGraph 驱动） ----------------

#: 阶段顺序 = runs.py STAGES 顺序。
STAGE_ORDER: list[str] = [
    "scan_media", "plan_segments", "understand_media", "build_timeline",
    "explore_web", "pick_music", "write_doc", "finishing_guide",
]

_STAGFN = {
    "scan_media": scan_media,
    "plan_segments": plan_segments,
    "understand_media": understand_media,
    "build_timeline": build_timeline,
    "explore_web": explore_web,
    "pick_music": pick_music,
    "write_doc": write_doc,
    "finishing_guide": finishing_guide,
}


def _initial_state(media_folder: str, copy: str) -> CutState:
    return {"media_folder": media_folder, "copy": copy,
            "log": [], "media": [], "segments": [],
            "timeline": [], "web_assets": [], "music": {}}


def run_sequential(media_folder: str, copy: str, *,
                   control: RunControl | None = None,
                   log: object | None = None,
                   run_dir: Path | None = None,
                   seed: int | None = None) -> CutState:
    """按阶段顺序执行（P8 驱动核心；runs.py 与 CLI 都走这里）。

    control/log/run_dir/seed 经 state["_ctx"] 注入节点；全为 None 时等价旧版一次性执行。
    RunHalted（暂停/取消）向外抛，由驱动方（runs.py）持久化状态并退出。
    """
    state = _initial_state(media_folder, copy)
    ctx = RunCtx(run_id="", control=control, log=log, run_dir=run_dir, seed=seed)
    state["_ctx"] = ctx
    for stage in STAGE_ORDER:
        state = {**state, **_STAGFN[stage](state)}
    return state


def run(media_folder: str, copy: str) -> dict:
    """跑完整流水线，返回最终 state（兼容入口；run_id="" 无控制）。"""
    return run_sequential(media_folder, copy)
