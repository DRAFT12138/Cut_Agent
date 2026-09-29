"""Explainable, deterministic rough-cut checks; at most one repair pass."""
from __future__ import annotations

from copy import deepcopy
import math
import re

from .shots import number, select_shot, valid_shots
from .vision import STILL_THRESH_ABS

ROLES = ("开头", "发展", "高潮", "收尾")


def structure(segments: list[dict]) -> list[dict]:
    result = deepcopy(segments)
    for i, segment in enumerate(result):
        default = "开头" if i == 0 else "收尾" if i == len(result) - 1 else "高潮" if i == len(result) - 2 else "发展"
        if segment.get("role") not in ROLES:
            segment["role"] = default
            segment["role_source"] = "rule"
        else:
            segment["role_source"] = "model"
        segment["intensity"] = min(5, max(1, number(segment.get("intensity"),
                                                      {"开头": 2, "发展": 3, "高潮": 5, "收尾": 2}[segment["role"]])))
    return result


def narration_seconds(text: str) -> float:
    return round(len(re.sub(r"[\s，。！？、；：,.!?;:]+", "", text)) / 4.5, 2)


def ranked_candidates(row: dict, pool: list[tuple]) -> list[tuple]:
    """Deterministic role/word overlap ranking, shared by repairs and alternatives."""
    def tokens(text):
        text = re.sub(r"\W", "", str(text)).lower()
        return {text[i:i + 2] for i in range(max(0, len(text) - 1))}
    query = tokens(str(row.get("segment_text") or "") + str(row.get("shot_description") or ""))
    ranked = []
    for m, shot in pool:
        if (m["name"], shot["idx"]) == (row.get("media"), row.get("shot_idx")):
            continue
        words = tokens(shot.get("description") or m.get("description", ""))
        overlap = len(query & words) / max(1, len(query | words))
        role_match = row.get("role") in shot.get("roles", [])
        same_media = m["name"] == row.get("media")
        score = overlap + .5 * role_match + .1 * same_media
        reason = " / ".join(filter(None, ["角色匹配" if role_match else "", "描述词组相近" if overlap else "",
                                          "同素材另一镜头" if same_media else "其他素材镜头"]))
        ranked.append((m, shot, score, reason))
    return sorted(ranked, key=lambda item: (-item[2], item[0]["name"], item[1]["idx"]))


def alternatives(rows: list[dict], media: list[dict]) -> None:
    """Rank up to three real alternatives using role, description overlap and source continuity."""
    pool = [(m, s) for m in media for s in valid_shots(m)]
    for row in rows:
        row["alternatives"] = [{
                "media": m["name"], "shot_idx": shot["idx"], "start": shot["start"], "end": shot["end"],
                "description": shot.get("description", ""), "score": round(score, 4),
                "reason": reason} for m, shot, score, reason in ranked_candidates(row, pool)[:3]]


def critique(timeline: list[dict], media: list[dict], *, auto_fix: bool = True):
    rows = deepcopy(timeline)
    pool = [(m, s) for m in media if m.get("kind") == "video" for s in valid_shots(m)]
    pool.sort(key=lambda pair: (pair[0]["name"], pair[1]["start"]))
    measured = [(m, s) for m, s in pool if math.isfinite(number(s.get("motion"), float("nan")))
                and number(s.get("motion")) >= 0]
    repairs, warnings = [], []
    preserved = None

    def identity(row):
        return row.get("media"), row.get("shot_idx")

    def motion(row):
        return next((number(s.get("motion")) for m, s in measured
                     if (m["name"], s["idx"]) == identity(row)), None)

    def hook_ok():
        if not rows or not measured:
            return False
        maximum = max(number(s.get("motion")) for _, s in measured)
        elapsed = 0.0
        for row in rows[:2]:
            value = motion(row)
            if elapsed < 3 and value is not None and value >= maximum:
                return True
            elapsed += number(row.get("use_duration"))
        return False

    def replace(index, candidate, rule):
        if rows[index] is preserved:
            return  # The original opening row must survive the entire repair pass.
        old = identity(rows[index])
        m, shot, score, reason = candidate
        rows[index] = select_shot({**rows[index], "shot_idx": shot["idx"], "span": False}, m)
        repairs.append({"rule": rule, "seq": index + 1, "before": list(old),
                        "after": list(identity(rows[index])), "score": round(score, 4), "reason": reason,
                        "message": f"第 {index + 1} 行画面由 {old[0]}#{old[1]} 换为 {m['name']}#{shot['idx']}（{reason}）；旁白保持不变"})

    def candidate(index, eligible):
        return next(iter(ranked_candidates(rows[index], eligible)), None)

    if auto_fix and rows and pool:
        if measured and not hook_ok() and rows[0].get("source", "local") == "local":
            maximum = max(number(s.get("motion")) for _, s in measured)
            best = candidate(0, [(m, s) for m, s in measured if number(s.get("motion")) == maximum])
            if best:
                m, shot, _, _ = best
                preserved = rows[0]
                index = next((i for i, row in enumerate(rows) if row.get("source", "local") == "local"
                              and identity(row) == (m["name"], shot["idx"])), None)
                if index is not None:
                    rows.insert(0, rows.pop(index))
                    message = f"原第 {index + 1} 行（画面与旁白一起）移至开头；原第 1 行完整后移到第 2 行，请复核旁白顺序"
                else:
                    hook = select_shot({"shot_idx": shot["idx"], "segment_text": "", "role": "开头",
                                        "role_source": "rule", "intensity": preserved.get("intensity", 3),
                                        "note": "自检新增无旁白开场画面；原第 1 行完整后移"}, m)
                    rows.insert(0, hook)
                    message = f"新增 {m['name']}#{shot['idx']} 无旁白开场（{hook['use_duration']:.2f}s）；原第 1 行完整后移到第 2 行，原旁白顺序保留"
                repairs.append({"rule": "opening_hook", "seq": 1, "mode": "move" if index is not None else "insert",
                                "before": list(identity(preserved)), "after": list(identity(rows[0])),
                                "preserved_seq": 2, "message": message})
        for i in range(1, len(rows)):
            if rows[i].get("media") and rows[i].get("media") == rows[i - 1].get("media"):
                if i == len(rows) - 1 and i > 1 and motion(rows[i]) is not None and motion(rows[i]) < STILL_THRESH_ABS:
                    replacement = candidate(i - 1, [pair for pair in pool if pair[0]["name"] not in
                                                    {rows[i - 2].get("media"), rows[i].get("media")}])
                    if replacement and rows[i - 1] is not preserved:
                        replace(i - 1, replacement, "adjacent_source")
                        continue
                forbidden = {rows[i - 1].get("media")}
                if i + 1 < len(rows):
                    forbidden.add(rows[i + 1].get("media"))
                replacement = candidate(i, [pair for pair in pool if pair[0]["name"] not in forbidden])
                if replacement:
                    replace(i, replacement, "adjacent_source")
        if len(rows) > 1 and rows[-1].get("source", "local") == "local" and (motion(rows[-1]) is None or motion(rows[-1]) >= STILL_THRESH_ABS):
            replacement = candidate(len(rows) - 1, [pair for pair in measured if number(pair[1].get("motion")) < STILL_THRESH_ABS
                                                   and pair[0]["name"] != rows[-2].get("media")])
            if replacement:
                replace(len(rows) - 1, replacement, "quiet_ending")

    def warn(rule, seq, message):
        warnings.append({"rule": rule, "seq": seq, "message": message})

    if rows and not hook_ok():
        warn("opening_hook", 1, "开头 3 秒内未确认最高运动镜头，请人工检查钩子")
    if rows and (motion(rows[-1]) is None or motion(rows[-1]) >= STILL_THRESH_ABS):
        warn("quiet_ending", len(rows), "结尾未确认低运动镜头，请人工确认收束")
    for i, row in enumerate(rows):
        row["seq"] = i + 1
        if i and row.get("media") and row.get("media") == rows[i - 1].get("media"):
            warn("adjacent_source", i + 1, "相邻两行使用同素材，无合适替换时保留并交人工确认")
        estimate = narration_seconds(str(row.get("segment_text", "")))
        row["narration_duration"] = estimate
        shortage = round(estimate - number(row.get("use_duration")), 2)
        if shortage > .05:
            warn("narration_shortfall", i + 1, f"画面短于旁白约 {shortage:.2f}s；建议跨镜头、补画面或改短旁白")
    intensities = [number(row.get("intensity"), 3) for row in rows]
    if len(intensities) >= 3 and (all(a <= b for a, b in zip(intensities, intensities[1:]))
                                or all(a >= b for a, b in zip(intensities, intensities[1:]))):
        warn("monotonic_emotion", None, "情绪强度单调，建议人工调整叙事起伏；未自动修改")
    alternatives(rows, media)
    return rows, {"repair_passes": 1 if auto_fix else 0, "repairs": repairs,
                  "warnings": warnings, "intensity_arc": intensities,
                  "disclosure": "规则依据运动度与素材重复性修复；候选按角色和描述词组排序，不能代替语义判断。钩子移行会连同旁白移动，新钩子不添加旁白且会增加总时长；原开场行完整保留。朗读时长为估算。"}
