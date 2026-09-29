"""Offline plan editing. plan.json is authoritative; documents are derived exports."""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import shutil
import tempfile
import time

from . import exports, graph, runs, runctl
from .craft import critique
from .delivery import PLATFORMS
from .shots import bind_local_source, select_shot
from .source_timing import align_source


def load_plan(run_id: str) -> dict:
    root = runctl.run_dir(run_id)
    if exports.pending(root):
        runs.recover_interrupted(run_id)
        if exports.pending(root):
            raise runctl.RunError("编辑导出尚未提交，恢复完成后重试")
    plan = runctl.read_json(root / "plan.json")
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise runctl.RunError("缺少有效的 version 1 plan.json")
    return plan


def _positive(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise runctl.RunError("时长必须是正数")
    if not math.isfinite(number) or number <= 0:
        raise runctl.RunError("时长必须是有限正数")
    return number


def validate(plan: dict) -> dict:
    result = deepcopy(plan)
    if result.get("platform", "douyin") not in PLATFORMS:
        raise runctl.RunError("不支持的平台预设")
    if not isinstance(result.get("media_folder"), str):
        raise runctl.RunError("plan 缺少素材目录 media_folder")
    if not isinstance(result.get("timeline"), list) or not isinstance(result.get("media"), list):
        raise runctl.RunError("plan 必须包含 timeline/media 数组")
    if any(not isinstance(m, dict) or not isinstance(m.get("name"), str)
           or m.get("kind") not in ("video", "image") for m in result["media"]):
        raise runctl.RunError("media 清单包含无效素材")
    media = {m["name"]: m for m in result["media"]}
    for i, row in enumerate(result["timeline"], 1):
        if not isinstance(row, dict):
            raise runctl.RunError("时间线行必须为对象")
        row["seq"] = i
        row["use_duration"] = _positive(row.get("use_duration"))
        try:
            offset = float(row.get("start_offset", 0))
        except (TypeError, ValueError):
            raise runctl.RunError("入点必须是非负数")
        if not math.isfinite(offset) or offset < 0:
            raise runctl.RunError("入点必须是有限非负数")
        if row.get("source", "local") == "local":
            item = media.get(row.get("media"))
            if item is None:
                raise runctl.RunError(f"未知本地素材：{row.get('media')}")
            row = bind_local_source(row, item)
            result["timeline"][i - 1] = row
            if item["kind"] == "video" and offset + row["use_duration"] > float(item["duration"]) + .001:
                raise runctl.RunError(f"第 {i} 行超过源素材时长")
        row.update(start_offset=offset, start=offset, end=offset + row["use_duration"])
        result["timeline"][i - 1] = align_source(row, media.get(row.get("media")))
    return result


def patch_plan(plan: dict, operation: dict) -> dict:
    result = deepcopy(plan)
    rows = result["timeline"]
    kind = operation.get("op")
    original_positions = list(range(1, len(rows) + 1))

    def index(key="row"):
        value = operation.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= len(rows):
            raise runctl.RunError(f"{key} 超出行号范围")
        return value - 1

    if kind == "move":
        source, target = index(), index("to")
        rows.insert(target, rows.pop(source))
        original_positions.insert(target, original_positions.pop(source))
    elif kind == "drop":
        target = index()
        rows.pop(target)
        original_positions.pop(target)
    elif kind == "duration":
        rows[index()]["use_duration"] = _positive(operation.get("seconds"))
    elif kind in ("swap", "append"):
        item = next((m for m in result["media"] if m["name"] == operation.get("media")), None)
        if item is None:
            raise runctl.RunError("未知素材，必须选择当前计划媒体清单中的文件")
        target = index() if kind == "swap" else len(rows)
        original = rows[target] if kind == "swap" else {"segment_text": "", "role": "发展", "intensity": 3}
        replacement = select_shot({**original, "shot_idx": operation.get("shot"), "span": False,
                                   "start_offset": 0}, item)
        if kind == "swap":
            rows[target] = replacement
        else:
            rows.append(replacement)
    else:
        raise runctl.RunError(f"未知编辑操作：{kind}")
    if kind in ("move", "drop"):
        current_positions = {old: new for new, old in enumerate(original_positions, 1)}
        for asset in result.get("web_assets", []):
            old = asset.get("for_seq")
            if isinstance(old, int) and not isinstance(old, bool):
                # Zero means the linked row was removed; keep the search record
                # without pointing its repair action at an unrelated row.
                asset["for_seq"] = current_positions.get(old, 0)
    return validate(result)


def rebuild(run_id: str, operation: dict | None = None, expected_revision: int | None = None,
            *, preview: bool = False) -> dict:
    if not (runctl.run_dir(run_id) / "plan.json").is_file():
        raise runctl.RunError("计划尚未生成")
    with runctl.RunLease(run_id):
        exports.recover(runctl.run_dir(run_id))
        return _rebuild_locked(run_id, operation, expected_revision, preview=preview)


def _rebuild_locked(run_id: str, operation: dict | None = None, expected_revision: int | None = None,
                    *, preview: bool = False) -> dict:
    """Also used by resume when an edited plan outlives its stage checkpoints."""
    meta = runs.status_of(run_id)
    if meta.get("status") not in ("done", "paused", "failed", "canceled", "interrupted"):
        raise runctl.RunError("任务仍在运行，不能编辑")
    original = load_plan(run_id)
    revision = original.get("revision", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise runctl.RunError("计划 revision 必须为非负整数")
    if expected_revision is not None and expected_revision != revision:
        raise runctl.RunError("计划已被修改，请刷新后重试")
    plan = patch_plan(original, operation) if operation else validate(original)
    plan["timeline"], plan["critique"] = critique(plan["timeline"], plan["media"], auto_fix=False)
    plan["revision"] = revision + 1
    root = runctl.run_dir(run_id).resolve()
    # Render everything before publishing a durable manifest. After publication
    # starts, retain the payload on every exception, including process death.
    staging = Path(tempfile.mkdtemp(prefix=".rebuild-", dir=root)).resolve()
    try:
        ctx = graph.RunCtx(run_id, run_dir=staging, options={"preview": preview, "finishing_llm": False})
        plan["_ctx"], plan["_export_root"] = ctx, str(root)
        rendered = graph.write_doc(plan)
        plan.update(rendered)
        finished = graph.finishing_guide(plan)
        output = runctl.read_json(staging / "plan.json")
        output.update(revision=plan["revision"], doc_path=str(root / "粗剪方案.md"),
                      line_doc_path=str(root / "cut_lines.txt"), guide_path=str(root / "精剪指导.md"))
        finished["guide_path"] = output["guide_path"]
        if (output.get("preview") or {}).get("status") == "ready":
            output["preview"]["path"] = str(root / "preview.mp4")
            rendered["preview"] = output["preview"]
        rendered.update(revision=output["revision"], doc_path=output["doc_path"], line_doc_path=output["line_doc_path"],
                        plan_path=str(root / "plan.json"))
        rendered.pop("log", None)
        finished.pop("log", None)
        # Every snapshot comes from the authoritative plan. In particular,
        # explore_web must never overwrite an edited timeline on later resume.
        snapshots = {
            "scan_media": {"media": output["media"]},
            "plan_segments": {"segments": output["segments"]},
            "understand_media": {"media": output["media"]},
            "build_timeline": {"timeline": output["timeline"], "critique": output["critique"]},
            "explore_web": {"timeline": output["timeline"], "critique": output["critique"], "web_assets": output["web_assets"]},
            "pick_music": {"music": output["music"]},
            "write_doc": rendered, "finishing_guide": finished,
        }
        entries = []
        for name, snapshot in snapshots.items():
            old = runctl.read_json(root / "stages" / f"{name}.json") or {}
            info = old.get("_stage", {}) if isinstance(old, dict) else {}
            info = dict(info) if isinstance(info, dict) else {}
            info.update(edited_revision=output["revision"], progress={"done": 1, "total": 1})
            info.setdefault("status", "done")
            info.setdefault("warnings", [])
            if name in ("write_doc", "finishing_guide"):
                info.update(status="degraded" if ctx.warnings else "done", warnings=list(ctx.warnings))
            snapshot["_stage"] = info
            runctl.write_json_atomic(staging / "stages" / f"{name}.json", snapshot)
            entries.append({"name": name, **info})
        persisted = {k: v for k, v in meta.items() if k not in ("stages_done", "export_pending")}
        persisted.update(status="done", stages=entries, resumable_from=None, error=None,
                         plan_revision=output["revision"], updated=time.time(), pid=None,
                         worker_lock_version=1, control_requested=None)
        runctl.write_json_atomic(staging / "run.json", persisted)
        runctl.write_json_atomic(staging / "plan.json", output)
        stamp = str(time.time_ns())
        runctl.write_json_atomic(root / "history" / f"plan-{stamp}.json", original)
        archive = root / "history/checkpoints" / stamp
        archive.mkdir(parents=True, exist_ok=True)
        for previous in (root / "stages").glob("*.json"):
            shutil.copy2(previous, archive / previous.name)
        exports.publish(root, staging, output["revision"])
    finally:
        if staging.exists() and not exports.pending(root):
            exports.discard_staging(root, staging)
    try:
        runctl.EventLog(run_id).event("write_doc", "log", msg="计划已离线重建", revision=output["revision"])
    except OSError:
        pass  # The edit is committed; log failure must not invite a duplicate edit.
    return output
