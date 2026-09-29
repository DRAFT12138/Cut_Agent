"""视觉层：变化驱动的自适应抽帧 + 视觉理解。

核心思路（不做均匀采样）：
  1. 候选帧流：按候选间隔（默认 0.25s）选原生帧，保存实际 PTS，
     逐帧算 8x8 RGB 缩略图，用相邻帧平均差（MAD）作为画面变化信号；
  2. 镜头切分：MAD 超过局部基线的 CUT_REL 倍且超过绝对噪声下限的上升沿判为切点，
     得到 shots（场景），硬切/转场都会在这里暴露；
  3. 预算分配：先保证每个已检测镜头 1 帧，再把目标预算（默认 12）的余量按变化量分配——
     静止镜头（画面变化小）只拿 1 帧代表，运镜/转场多/动作大的镜头拿更多，
     镜头数超过目标预算时保留最低覆盖，不合并真实镜头边界来省帧。
  4. 帧提取：用单条 ffmpeg select 表达式按 pts_time 一次抽出全部选中帧（1 次解码）；
  5. 理解：把每个镜头抽到的帧（带时间戳）一次性发给本地多模态 LLM，
     得到镜头级画面描述（主体/环境/运动/色调），供时间线编排做"按内容选素材"。
     LLM 不可用时返回纯几何信息（shots + 帧路径），不阻塞主流程。

依赖：ffmpeg（内置 tools/ 优先）+ Pillow（仅缩略图）；LLM 走 llm.py 的 OpenAI 兼容端点。
"""
from __future__ import annotations

import base64
import json
import hashlib
import math
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .llm import LLMError, chat_vision, parse_json_lenient
from .runctl import retry_checkpoint
from .media import _ffmpeg, video_fit_filter

# ---- 默认参数（可用环境变量覆盖，便于调参）----
import os

CANDIDATE_STEP = float(os.environ.get("CUT_AGENT_VISION_CAND_STEP", "0.25"))   # 候选帧间隔
MAX_CANDIDATES = int(os.environ.get("CUT_AGENT_VISION_MAX_CAND", "600"))       # 候选帧上限（长视频自动放宽步长）
CUT_REL = float(os.environ.get("CUT_AGENT_VISION_CUT_REL", "12"))              # 硬切判据：MAD 超过局部基线 12 倍
CUT_ABS_MIN = float(os.environ.get("CUT_AGENT_VISION_CUT_ABS", "2.0"))         # 硬切绝对下限（8x8 RGB 压缩噪声 ~1.5）
STILL_THRESH_ABS = float(os.environ.get("CUT_AGENT_VISION_STILL", "2.5"))      # 镜头运动量低于此值视为"静止"
STILL_THRESH_REL = float(os.environ.get("CUT_AGENT_VISION_STILL_REL", "0.10")) # 或低于全片最大运动量的 10%
BUDGET_MAX = int(os.environ.get("CUT_AGENT_VISION_BUDGET", "12"))              # 目标预算，至少每个镜头 1 帧
FRAME_LONG_EDGE = int(os.environ.get("CUT_AGENT_VISION_EDGE", "640"))          # 送 LLM 的帧长边
FRAME_FFMPEG_Q = 3
STATIC_LONG = float(os.environ.get("CUT_AGENT_VISION_STATIC_LONG", "20"))      # 静止镜头超过 20s 补一帧复核
FRAME_SPACING = 0.4                                                             # 镜头内帧最小间距（秒）
GEOMETRY_VERSION = 5  # square-pixel display geometry and bounded portrait frames


def geometry_settings() -> dict:
    return {name: globals()[name] for name in (
        "GEOMETRY_VERSION", "CANDIDATE_STEP", "MAX_CANDIDATES", "CUT_REL", "CUT_ABS_MIN",
        "STILL_THRESH_ABS", "STILL_THRESH_REL", "BUDGET_MAX",
        "FRAME_LONG_EDGE", "FRAME_FFMPEG_Q", "STATIC_LONG", "FRAME_SPACING")}

DEFAULT_VISION_PROMPT = (
    "你是粗剪素材理解员。给你同一段视频的若干帧（按时间顺序，标注了秒数），"
    "请用中文输出：1) 画面主体与内容；2) 环境与场景；3) 镜头运动（静止/推拉/摇移/运镜/硬切转场）；"
    "4) 色调与氛围；5) 适合搭配什么旁白/情绪。总长 120 字以内，直接输出 JSON。"
)


class VisionError(RuntimeError):
    pass


# ---------------- 几何层（不依赖 LLM） ----------------

@dataclass
class Shot:
    idx: int                 # 镜头序号（从 1 起）
    start: float             # 起（秒）
    end: float               # 止（秒）
    cut_score: float         # 切点处的变化强度（0 表示片头）
    motion: float            # 镜头内平均帧差（越大越动态）
    frames: list[Path] = field(default_factory=list)   # 抽出的代表帧路径
    frame_times: list[float] = field(default_factory=list)
    description: str = ""
    roles: list[str] = field(default_factory=list)
    frame_motions: list[float] = field(default_factory=list)
    # Model-derived facts stay separate from prose so callers can filter and
    # rank them.  Each value carries its evidence time and confidence.
    tags: dict[str, list[str]] = field(default_factory=dict)
    tag_evidence: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"idx": self.idx, "start": self.start, "end": self.end,
                "cut_score": round(self.cut_score, 4), "motion": round(self.motion, 4),
                "n_frames": len(self.frames), "frames": [str(p) for p in self.frames],
                "frame_times": list(self.frame_times), "frame_motions": list(self.frame_motions),
                "description": self.description, "roles": list(self.roles),
                "tags": {k: list(v) for k, v in self.tags.items()},
                "tag_evidence": [dict(v) for v in self.tag_evidence]}


@dataclass
class VideoVision:
    name: str
    path: Path
    duration: float
    shots: list[Shot] = field(default_factory=list)
    frames: list[Path] = field(default_factory=list)
    frame_times: list[float] = field(default_factory=list)
    description: str = ""
    scene_cuts: list[float] = field(default_factory=list)
    geometry_reused: bool = False
    sampling: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict, src: Path):
        shots = [Shot(idx=s["idx"], start=s["start"], end=s["end"], cut_score=s["cut_score"],
                      motion=s["motion"], frames=[Path(p) for p in s["frames"]],
                      frame_times=s["frame_times"], frame_motions=s["frame_motions"],
                      description=s.get("description", ""), roles=s.get("roles", []),
                      tags=s.get("tags", {}), tag_evidence=s.get("tag_evidence", []))
                 for s in data["shots"]]
        duration = float(data["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid cached duration")
        for shot in shots:
            if (not isinstance(shot.idx, int) or isinstance(shot.idx, bool) or shot.idx < 1
                    or not 0 <= shot.start < shot.end <= duration
                    or not shot.frames or len(shot.frames) != len(shot.frame_times)
                    or len(shot.frames) != len(shot.frame_motions)
                    or any(not shot.start <= t < shot.end for t in shot.frame_times)
                    or any(not math.isfinite(m) or not 0 <= m <= 255 for m in shot.frame_motions)):
                raise ValueError("invalid cached shot/frame geometry")
        if len({shot.idx for shot in shots}) != len(shots):
            raise ValueError("duplicate cached shot ID")
        return cls(src.name, src, duration, shots=shots,
                   frames=[p for shot in shots for p in shot.frames],
                   frame_times=[t for shot in shots for t in shot.frame_times],
                   description=data.get("description", ""), scene_cuts=data.get("scene_cuts", []),
                   sampling=data.get("sampling", {}))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "duration": self.duration,
            "scene_cuts": list(self.scene_cuts),
            "n_shots": len(self.shots),
            "shots": [s.to_dict() for s in self.shots],
            "frames": [str(f) for f in self.frames],
            "frame_times": list(self.frame_times),
            "description": self.description,
            "sampling": dict(self.sampling),
        }


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _candidate_step(duration: float) -> float:
    """保证候选帧数不超 MAX_CANDIDATES（长视频自动降采样）。"""
    if duration <= 0:
        return CANDIDATE_STEP
    return max(CANDIDATE_STEP, duration / MAX_CANDIDATES)


def _extract_rgb_thumbs(src: Path, step: float, work_dir: Path) -> list[tuple[float, "Image.Image"]]:
    """按时间桶选原视频帧，保留真实 PTS，输出 64x36 RGB JPEG。

    用 RGB 而非灰度：同亮度异色相的硬切（红墙→绿墙）在灰度上不可见。
    """
    tag = _source_tag(src)
    cdir = work_dir / f"cand_{tag}"
    cdir.mkdir(parents=True, exist_ok=True)
    # 清掉上次残留
    for f in cdir.glob("c_*.jpg"):
        f.unlink(missing_ok=True)
    # fps= resampling renumbers frames at a synthetic clock and can select a
    # nearby source frame. Select the first native frame in each time bucket
    # instead; showinfo after selection logs <= MAX_CANDIDATES actual PTS values.
    expr = f"isnan(prev_selected_t)+gt(floor((t+0.000001)/{step:.4f})\\,floor((prev_selected_t+0.000001)/{step:.4f}))"
    r = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "info", "-y",
         "-i", str(src),
         "-vf", f"setpts=PTS-STARTPTS,select='{expr}',scale=64:36:flags=area,showinfo",
         "-fps_mode", "vfr",
         str(cdir / "c_%05d.jpg")],
        capture_output=True, text=True,
        timeout=max(120, step * MAX_CANDIDATES * 4),
    )
    if r.returncode != 0:
        raise VisionError(f"候选帧提取失败 {src.name}: {r.stderr[-300:]}")
    base = re.search(r"config in time_base:\s*(\d+)/(\d+)", r.stderr)
    pts = {int(index): int(value) for index, value in re.findall(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)", r.stderr)}
    if not base or not pts or int(base[2]) == 0:
        raise VisionError(f"候选帧缺少真实时间信息：{src.name}")
    time_base = int(base[1]) / int(base[2])
    out: list[tuple[float, Image.Image]] = []
    for i, p in enumerate(sorted(cdir.glob("c_*.jpg"))):
        try:
            if i in pts:
                out.append((pts[i] * time_base, Image.open(p).convert("RGB")))
        except Exception:
            continue
    return out


def _mad(img_a: "Image.Image", img_b: "Image.Image") -> float:
    """8x8 RGB 缩略图的逐通道平均绝对差，范围 [0,255]。"""
    a = img_a.resize((8, 8))
    b = img_b.resize((8, 8))
    pa = a.convert("RGB").tobytes()
    pb = b.convert("RGB").tobytes()
    return sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)


def _find_cuts(mads: list[float], times: list[float], duration: float) -> list[tuple[float, float]]:
    """在 MAD 曲线上找镜头切点，返回 [(切点时间, 变化强度), ...]。

    判据（自适应，防误报/漏报）：
    - 硬切：mads[i] 高于"局部基线 × CUT_REL(12)"——前后各 5 个候选点（排除自身）的中位数。
      平稳运镜的 MAD 在自身基线附近波动（±50% 都正常），而硬切通常是基线的几十倍，
      相对判据对"整段都动"和"整段都静"都稳；
    - 绝对下限 CUT_ABS_MIN（8x8 RGB 的 JPEG 压缩噪声大约 <1.5）：
      全片静止时 base≈0.3，12×base 会低于噪声，由绝对下限兜底；
    - 只计上升沿（mads[i] 跳起而 mads[i-1] 还在基线）——一次硬切只数一个切点；
    - 相邻 1.0s 内的切点合并，保留更强的。
    切点时间取"新画面第一帧"的候选时间（step 内的精度）。
    """
    n = len(mads)
    if n < 4:
        return []
    half = 5
    cuts: list[tuple[float, float]] = []
    for i in range(1, n):
        lo = max(0, i - half)
        hi = min(n, i + half)
        window = mads[lo:hi]
        window.pop(min(i - lo, len(window) - 1))  # 排除候选点自身
        base = _percentile(sorted(window), 0.5)
        thresh = max(base * CUT_REL, CUT_ABS_MIN)
        if mads[i] > thresh and mads[i - 1] <= thresh:
            cuts.append((times[i], mads[i]))
    merged: list[tuple[float, float]] = []
    for t, s in cuts:
        if merged and t - merged[-1][0] <= 1.0:
            if s > merged[-1][1]:
                merged[-1] = (t, s)
        else:
            merged.append((t, s))
    return merged


def _is_still(seg: dict, global_max_motion: float) -> bool:
    """镜头是否"静止"：运动量同时低于绝对阈值与全片最大值的相对阈值。"""
    m = seg["motion"]
    return m < STILL_THRESH_ABS and (global_max_motion <= 0 or m < global_max_motion * STILL_THRESH_REL)


def _allocate(mads: list[float], times: list[float],
              cuts: list[tuple[float, float]], duration: float,
              budget: int) -> list[dict]:
    """按镜头信息量分配帧预算，返回 [{"t":秒}, ...]（升序，全局去重）。

    规则（变化驱动，不是均匀采样）：
    - 静止镜头（motion 低）：只给 1 帧代表帧（画面几乎不变，多抽没信息）；
      例外：静止镜头超过 STATIC_LONG 秒时在 50% 处补 1 帧复核（防漏掉慢速变化）；
    - 动态镜头：至少 1 帧，剩余预算按 weight = motion^0.75 比例分给各动态镜头；
      镜头内的帧挑在"变化峰"（局部最大 MAD）附近——动作发生处信息最大；
    - 峰间留 FRAME_SPACING 秒；总数不超过 max(budget, 镜头数)，覆盖优先。
    """
    if duration <= 0 or budget <= 0:
        return []
    bounds = [0.0] + [t for t, _ in cuts] + [duration]
    shots = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]

    def seg_of(start: float, end: float) -> tuple[list[float], list[float]]:
        i0 = next((i for i, t in enumerate(times) if t >= start - 1e-6), 0)
        i1 = next((i for i in range(i0, len(times)) if times[i] >= end - 1e-6), len(times))
        return mads[i0:i1], times[i0:i1]

    segs = []
    for s, e in shots:
        sm, st = seg_of(s, e)
        motion = (sum(sm) / len(sm)) if sm else 0.0
        segs.append({"start": s, "end": e, "motion": motion, "mads": sm, "times": st})
    gmax = max((sg["motion"] for sg in segs), default=0.0)
    for sg in segs:
        sg["still"] = _is_still(sg, gmax)

    # The shot inventory retains every detected boundary. Merging only the
    # allocation groups silently left some of those shots without a frame.
    budget = max(budget, len(segs))

    # 1) 每个镜头先拿代表帧（静止镜头到此为止）
    alloc = {id(sg): 1 for sg in segs}
    for sg in segs:
        if sg["still"] and sg["end"] - sg["start"] > STATIC_LONG and sum(alloc.values()) < budget:
            alloc[id(sg)] += 1  # 长静止镜头补一帧复核
    used = sum(alloc.values())
    # 2) 剩余预算按 motion 权重分给动态镜头（比例 + 最大余数法）
    remain = budget - used
    actives = [sg for sg in segs if not sg["still"]]
    if actives and remain > 0:
        weights = {id(sg): max(1e-3, sg["motion"] ** 0.75) for sg in actives}
        total_w = sum(weights.values())
        raw = {id(sg): remain * weights[id(sg)] / total_w for sg in actives}
        base = {i: int(v) for i, v in raw.items()}
        leftover = remain - sum(base.values())
        for i, _ in sorted(raw.items(), key=lambda kv: kv[1] - int(kv[1]),
                           reverse=True)[:leftover]:
            base[i] += 1
        for sg in actives:
            alloc[id(sg)] += base[id(sg)]
    # 3) 每个镜头挑具体帧
    picked: list[float] = []
    for sg in segs:
        k = alloc[id(sg)]
        sm, st = sg["mads"], sg["times"]
        if not st:
            picked.append(min(sg["start"] + 0.2, max(0.0, sg["end"] - 0.05)))
            continue
        # 代表帧：镜头中点（静止镜头只取这一帧）
        rep_t = min(sg["start"] + (sg["end"] - sg["start"]) * 0.4,
                    max(0.0, sg["end"] - 0.1))
        # 吸附到最近的候选帧时间
        rep_t = min(st, key=lambda t: abs(t - rep_t))
        chosen: list[float] = [rep_t]
        if k > 1 and not sg["still"]:
            # 在镜头内找 (k-1) 个变化峰（MAD 局部最大），避开代表帧附近
            peaks: list[float] = []
            for j in range(1, len(sm) - 1):
                v = sm[j]
                if v >= sm[j - 1] and v >= sm[j + 1] and v > 0:
                    peaks.append((v, st[j]))
            peaks.sort(reverse=True)  # 强峰优先
            for v, t in peaks:
                if len(chosen) >= k:
                    break
                if all(abs(t - c) >= FRAME_SPACING for c in chosen):
                    chosen.append(t)
            # 峰不够（如匀速运动）：在镜头内按剩余名额均匀补
            j = 1
            while len(chosen) < k and j <= k:
                t = sg["start"] + (sg["end"] - sg["start"]) * j / (k + 1)
                if all(abs(t - c) >= FRAME_SPACING for c in chosen):
                    chosen.append(t)
                j += 1
        elif k > 1:  # 长静止镜头的复核帧
            t = sg["start"] + (sg["end"] - sg["start"]) * 0.7
            if all(abs(t - c) >= FRAME_SPACING for c in chosen):
                chosen.append(t)
        picked.extend(chosen)
    picked.sort()
    out = []
    for t in picked:
        t = min(max(0.0, t), max(0.0, duration - 0.05))
        if not out or t - out[-1] >= 0.15:
            out.append(t)
    return [{"t": t} for t in out[:budget]]


def _extract_frames(src: Path, picks: list[tuple[int, float]], step: float,
                    work_dir: Path) -> list[Path | None]:
    """按候选帧的真实时间抽代表帧，单遍解码，不改变源帧时钟。

    picks 的第一项是候选索引，第二项是原帧 PTS（相对首帧）。step 保留为调用兼容参数。
    输出与 picks 升序一一对应。长边缩到 FRAME_LONG_EDGE，q=3（小文件送 LLM）。
    """
    if not picks:
        return []
    tag = _source_tag(src)
    fdir = work_dir / f"vf_{tag}"
    fdir.mkdir(parents=True, exist_ok=True)
    for f in fdir.glob("f_*.jpg"):
        f.unlink(missing_ok=True)
    expr = "+".join(f"lt(abs(t-{t:.12f})\\,0.0000001)" for _, t in picks)
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-vf", f"setpts=PTS-STARTPTS,select='{expr}',{video_fit_filter(FRAME_LONG_EDGE, FRAME_LONG_EDGE)}",
           "-fps_mode", "vfr", "-q:v", str(FRAME_FFMPEG_Q), str(fdir / "f_%02d.jpg")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=max(120, len(picks) * 30))
    if r.returncode != 0:
        raise VisionError(f"代表帧提取失败 {src.name}: {r.stderr[-300:]}")
    if len(list(fdir.glob("f_*.jpg"))) != len(picks):
        raise VisionError(f"代表帧数量与原生时间不一致：{src.name}")
    # 与 picks 位置对齐：image2 muxer 从 1 开始编号，第 i 个 pick → f_{i+1}
    out: list[Path | None] = []
    for i in range(len(picks)):
        p = fdir / f"f_{i + 1:02d}.jpg"
        out.append(p if p.exists() and p.stat().st_size > 500 else None)
    return out


def _source_tag(src: Path) -> str:
    return re.sub(r"[^\w]", "_", src.stem)[:50] + "_" + hashlib.sha256(
        str(src.resolve()).encode("utf-8")).hexdigest()[:12]


def analyse_video(src: Path, duration: float,
                  work_dir: Path | None = None) -> VideoVision:
    """几何分析：候选帧曲线 → 镜头切分 → 预算分配 → 抽代表帧。不依赖 LLM。"""
    from .config import WORK_DIR
    work_dir = work_dir or (WORK_DIR / "vision")
    work_dir.mkdir(parents=True, exist_ok=True)
    vv = VideoVision(name=src.name, path=src, duration=duration)
    if duration <= 0:
        return vv
    step = round(_candidate_step(duration), 4)
    cand = _extract_rgb_thumbs(src, step, work_dir)
    if not cand:
        return vv
    times = [t for t, _ in cand]
    imgs = [im for _, im in cand]
    # Index i measures the change *into* frame i, so cut times refer to the new
    # scene's first sampled frame, not the preceding frame.
    mads = [0.0] + [_mad(imgs[i - 1], imgs[i]) for i in range(1, len(imgs))]
    cuts = _find_cuts(mads, times, duration)
    vv.scene_cuts = [t for t, _ in cuts]
    # A cut's inter-scene difference is not motion within either shot.
    cut_times = {t for t, _ in cuts}
    motions = [0.0 if t in cut_times else value for t, value in zip(times, mads)]
    picks_t = _allocate(motions, times, cuts, duration, BUDGET_MAX)
    # 吸附到实际候选帧并按候选索引去重，保留原生时间。
    picks: list[tuple[int, float]] = []
    for pk in picks_t:
        i = min(range(len(times)), key=lambda index: abs(times[index] - pk["t"]))
        if all(i != index for index, _ in picks):
            picks.append((i, times[i]))
    frames = _extract_frames(src, picks, step, work_dir)
    pairs = [(f, t) for f, t in zip(frames, [t for _, t in picks]) if f is not None]
    # 镜头归属
    bounds = [0.0] + [t for t, _ in cuts] + [duration]
    for s_idx in range(len(bounds) - 1):
        s_start, s_end = bounds[s_idx], bounds[s_idx + 1]
        seg_m = [m for m, t in zip(motions, times) if s_start <= t < s_end]
        shot = Shot(idx=s_idx + 1, start=s_start, end=s_end,
                    cut_score=next((c[1] for c in cuts if abs(c[0] - s_start) < 1e-6), 0.0),
                    motion=(sum(seg_m) / len(seg_m)) if seg_m else 0.0)
        shot.frames = [f for f, t in pairs if s_start - 1e-6 <= t < s_end - 1e-6]
        shot.frame_times = [t for _, t in pairs if s_start - 1e-6 <= t < s_end - 1e-6]
        motion_by_time = dict(zip(times, motions))
        shot.frame_motions = [motion_by_time[t] for t in shot.frame_times]
        vv.shots.append(shot)
    vv.frames = [f for f, _ in pairs]
    vv.frame_times = [t for _, t in pairs]
    vv.sampling = {"target_budget": BUDGET_MAX,
                   "effective_budget": max(BUDGET_MAX, len(vv.shots)) if BUDGET_MAX > 0 else 0,
                   "detected_shots": len(vv.shots), "covered_shots": sum(bool(s.frames) for s in vv.shots),
                   "missing_shots": [s.idx for s in vv.shots if not s.frames], "candidate_frames": len(cand)}
    return vv


# ---------------- LLM 视觉理解 ----------------

def _img_part_b64(p: Path) -> dict:
    b64 = base64.b64encode(p.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def describe_video(vv: VideoVision, prompt: str | None = None,
                   max_tokens: int = 900) -> str:
    """把代表帧 + 时间戳发给本地多模态 LLM，返回镜头描述文本。

    失败（服务未启/不支持/超时）返回 ""，由调用方降级为纯几何信息。
    """
    if not vv.frames:
        return ""
    user_parts: list[dict] = [{"type": "text", "text":
        f"这是视频 {vv.name}（{vv.duration:.1f}s）按画面变化抽出的 {len(vv.frames)} 帧，"
        f"共 {len(vv.shots)} 个镜头。镜头边界：{[f'{s.start:.1f}-{s.end:.1f}s' for s in vv.shots]}。"
        + (prompt or DEFAULT_VISION_PROMPT)}]
    for t, f in zip(vv.frame_times, vv.frames):
        user_parts.append({"type": "text", "text": f"[{t:.1f}s]"})
        user_parts.append(_img_part_b64(f))
    try:
        return chat_vision(
            "你是视频画面标注员，用中文回答，输出要具体（主体/环境/运动/色调），不要空话。",
            user_parts, temperature=0.2, max_tokens=max_tokens,
        )
    except LLMError:
        return ""


def analyse_with_llm(src: Path, duration: float,
                     work_dir: Path | None = None,
                     prompt: str | None = None) -> VideoVision:
    """几何分析 + LLM 镜头描述（LLM 失败自动降级，不抛异常）。"""
    from .config import WORK_DIR
    from .mediacache import GeometryCache
    from .checkpoints import fingerprint
    cache = GeometryCache(WORK_DIR / "media_cache")
    key = {"path": str(src.resolve()), "file": fingerprint(src),
           "duration": duration, "settings": geometry_settings()}
    destination = (work_dir or WORK_DIR / "vision") / "geometry"
    saved = cache.read(key, destination)
    vv = None
    if saved:
        try:
            vv = VideoVision.from_dict(saved["row"], src)
            vv.geometry_reused = True
        except (KeyError, TypeError, ValueError):
            pass
    if vv is None:
        vv = analyse_video(src, duration, work_dir)
        cache.write(key, {"row": vv.to_dict()})
    # Cache is committed before any model request/cancellation. A new run may
    # retry semantic labels even when a previous model call was unavailable.
    retry_checkpoint()
    if vv.frames:
        describe_shots(vv, prompt)
        descriptions = [f"镜头{s.idx}：{s.description}" for s in vv.shots if s.description]
        summary = "；".join(descriptions)
        vv.description = (describe_video(vv, prompt) or summary) if any(
            not s.description for s in vv.shots) else summary
    return vv


def describe_shots(vv: VideoVision, prompt: str | None = None) -> None:
    """Label up to four shots per request; missing/invalid labels stay empty."""
    available = [s for s in vv.shots if s.frames]
    for start in range(0, len(available), 4):
        retry_checkpoint()
        batch = available[start:start + 4]
        parts = [{"type": "text", "text":
                  '逐镜头描述主体、动作、情绪、色调，不要合并镜头。只输出 JSON：'
                  '{"shots":[{"shot_idx":1,"description":"具体画面",'
                  '"roles":["开头","发展","高潮","收尾"],'
                  '"tags":{"subjects":[],"actions":[],"scenes":[],"shot_sizes":[],'
                  '"camera_angles":[],"movement_directions":[],"gaze_directions":[],'
                  '"text_regions":[],"emotions":[]},'
                  '"evidence":[{"tag":"人物","frame_time":1.2,"confidence":0.9}]}]}。'
                  '标签必须来自画面；置信度为 0 到 1，frame_time 必须是所给帧时间。'
                  'roles 只选适合的角色，不必全部选择。' + (prompt or "")}]
        for shot in batch:
            parts.append({"type": "text", "text":
                          f"镜头 {shot.idx} [{shot.start:.2f}, {shot.end:.2f}) 秒"})
            # Preserve some movement evidence while bounding request size.
            indexes = sorted({0, len(shot.frames) // 2, len(shot.frames) - 1})
            for index in indexes:
                parts.append(_img_part_b64(shot.frames[index]))
        try:
            raw = chat_vision("你是视频镜头标注员，仅描述提供的画面。", parts, max_tokens=1800)
            data = parse_json_lenient(raw)
        except (LLMError, ValueError, TypeError):
            continue
        labels = data.get("shots", []) if isinstance(data, dict) else data
        if not isinstance(labels, list):
            continue
        by_id = {s.idx: s for s in batch}
        for label in labels:
            if not isinstance(label, dict):
                continue
            try:
                shot = by_id.get(int(label.get("shot_idx")))
            except (TypeError, ValueError):
                continue
            description = label.get("description")
            if shot is None or not isinstance(description, str) or not description.strip():
                continue
            shot.description = description.strip()
            roles = label.get("roles", [])
            shot.roles = [r for r in roles if r in ("开头", "发展", "高潮", "收尾")] if isinstance(roles, list) else []
            allowed = {"subjects", "actions", "scenes", "shot_sizes", "camera_angles",
                       "movement_directions", "gaze_directions", "text_regions", "emotions"}
            tags = label.get("tags", {})
            if isinstance(tags, dict):
                shot.tags = {key: [str(value).strip() for value in values
                                   if isinstance(value, (str, int, float)) and str(value).strip()]
                             for key, values in tags.items()
                             if key in allowed and isinstance(values, list)}
            evidence = label.get("evidence", [])
            valid_times = shot.frame_times
            shot.tag_evidence = []
            if isinstance(evidence, list):
                for item in evidence:
                    if not isinstance(item, dict) or not str(item.get("tag", "")).strip():
                        continue
                    try:
                        confidence, frame_time = float(item["confidence"]), float(item["frame_time"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if 0 <= confidence <= 1 and valid_times and any(abs(frame_time - t) < .011 for t in valid_times):
                        shot.tag_evidence.append({"tag": str(item["tag"]).strip(),
                                                  "frame_time": frame_time,
                                                  "confidence": confidence,
                                                  "source": "vision_model"})
