"""Durable, plan-aware human checks for the finishing handoff."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
import time

from . import runctl
from .editing import load_plan
from .finishing import markdown


def _source(run_id: str) -> tuple[dict, dict, str, list[str]]:
    plan = load_plan(run_id)
    guide = plan.get("finishing")
    if not isinstance(guide, dict) or not isinstance(guide.get("checklist"), list):
        raise runctl.RunError("精剪指导尚未生成")
    texts = [item.get("text") for item in guide["checklist"] if isinstance(item, dict)]
    if len(texts) != len(guide["checklist"]) or any(not isinstance(text, str) for text in texts):
        raise runctl.RunError("精剪指导的核对清单无效")
    digest = sha256(json.dumps(guide, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                               allow_nan=False).encode("utf-8")).hexdigest()
    return plan, guide, digest, texts


def _read(run_id: str, plan: dict, digest: str, texts: list[str]) -> dict:
    path = runctl.run_dir(run_id) / "review.json"
    saved = runctl.read_json(path)
    if path.exists() and not isinstance(saved, dict):
        raise runctl.RunError("人工核对记录损坏，请保留文件并修复后重试")
    if saved is not None:
        valid = (saved.get("schema_version") == 1
                 and type(saved.get("plan_revision")) is int and saved["plan_revision"] >= 0
                 and isinstance(saved.get("guide_digest"), str)
                 and re.fullmatch(r"[0-9a-f]{64}", saved["guide_digest"]) is not None
                 and isinstance(saved.get("checked"), list)
                 and all(type(value) is bool for value in saved["checked"])
                 and type(saved.get("updated_at")) in (int, float)
                 and math.isfinite(saved["updated_at"]) and saved["updated_at"] > 0)
        if not valid or (saved["guide_digest"] == digest and len(saved["checked"]) != len(texts)):
            raise runctl.RunError("人工核对记录损坏，请保留文件并修复后重试")
    matches = saved is not None and saved["guide_digest"] == digest
    checked = saved["checked"] if matches else [False] * len(texts)
    return {"plan_revision": plan.get("revision", 0), "guide_digest": digest,
            "items": [{"text": text, "checked": value} for text, value in zip(texts, checked)],
            "updated_at": saved.get("updated_at") if matches else None,
            "reset_for_new_guide": bool(saved) and not matches}


def get(run_id: str) -> dict:
    plan, _, digest, texts = _source(run_id)
    return _read(run_id, plan, digest, texts)


def set_check(run_id: str, expected_revision: int, index: int, text: str, checked: bool) -> dict:
    # Recover any interrupted export before taking the edit/review lease.
    load_plan(run_id)
    with runctl.RunLease(run_id):
        plan, _, digest, texts = _source(run_id)
        state = _read(run_id, plan, digest, texts)
        if expected_revision != state["plan_revision"]:
            raise runctl.RunError("计划已更新，请刷新核对清单")
        if type(index) is not int or not 0 <= index < len(texts) or texts[index] != text:
            raise runctl.RunError("核对项已变化，请刷新后重试")
        if type(checked) is not bool:
            raise runctl.RunError("checked 必须为布尔值")
        values = [item["checked"] for item in state["items"]]
        values[index] = checked
        updated_at = time.time()
        runctl.write_json_atomic(runctl.run_dir(run_id) / "review.json",
                                 {"schema_version": 1, "plan_revision": state["plan_revision"],
                                  "guide_digest": digest, "checked": values, "updated_at": updated_at})
        return _read(run_id, plan, digest, texts)


def export_markdown(run_id: str) -> str:
    plan, guide, digest, texts = _source(run_id)
    state = _read(run_id, plan, digest, texts)
    marked = deepcopy(guide)
    for item, review_item in zip(marked["checklist"], state["items"]):
        item["checked"] = review_item["checked"]
    count = sum(item["checked"] for item in state["items"])
    updated = (datetime.fromtimestamp(state["updated_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
               if state["updated_at"] else "尚未人工勾选")
    body = markdown(marked)
    header = (f"# 精剪指导（人工核对记录）\n\n"
              f"任务 {run_id} · 方案修订 {state['plan_revision']} · 已核对 {count}/{len(texts)} 项 · {updated}\n\n"
              "未勾选项目仍需人工检查。将本文件与粗剪方案及 frames/ 放在同一目录，保持相对链接有效。\n")
    return body.replace("# 精剪指导\n", header, 1)
