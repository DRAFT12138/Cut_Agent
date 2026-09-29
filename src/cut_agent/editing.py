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
        source = row.get("source", "local")
        if source == "generated":
            if row.get("kind") != "html" or not isinstance(row.get("local_path"), str):
                raise runctl.RunError(f"第 {i} 行不是有效的 HTML 动画素材")
            if (row.get("width"), row.get("height")) not in ((1920, 1080), (3840, 2160)):
                raise runctl.RunError(f"第 {i} 行 HTML 动画分辨率无效")
            asset = Path(row["local_path"]).resolve()
            # Persisted plans do not need to expose their run id: all generated files
            # must at least live under a directory named generated and exist.
            if asset.parent.name != "generated" or asset.suffix.lower() != ".html" or not asset.is_file():
                raise runctl.RunError(f"第 {i} 行 HTML 动画文件无效")
            pages = row.get("html_pages") or [{"path": row["local_path"], "duration": row["use_duration"]}]
            if not isinstance(pages, list) or not 1 <= len(pages) <= 6:
                raise runctl.RunError(f"第 {i} 行 HTML 动画画面数量无效")
            page_duration = 0.0
            for page in pages:
                page_path = Path(str(page.get("path", ""))).resolve() if isinstance(page, dict) else Path()
                if (not isinstance(page, dict) or page_path.parent.name != "generated"
                        or page_path.suffix.lower() != ".html" or not page_path.is_file()
                        or _positive(page.get("duration")) <= 0):
                    raise runctl.RunError(f"第 {i} 行包含无效 HTML 画面")
                page_duration += float(page["duration"])
            if not math.isclose(page_duration, row["use_duration"], abs_tol=.001, rel_tol=0):
                raise runctl.RunError(f"第 {i} 行 HTML 画面时长合计不匹配")
        elif source == "local":
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


def add_html_motion(run_id: str, *, after: int, prompt: str, duration: float,
                    resolution: str = "4k", expected_revision: int | None = None,
                    preview: bool = False, music_mode: str = "auto",
                    bpm: float | None = None) -> dict:
    """Generate an HTML motion clip and insert it after a timeline row."""
    duration = _positive(duration)
    if duration > 30:
        raise runctl.RunError("HTML 动画时长不能超过 30 秒")
    if not isinstance(after, int) or isinstance(after, bool):
        raise runctl.RunError("插入位置必须是行号")
    if len(prompt) > 4000:
        raise runctl.RunError("动画要求不能超过 4000 字")
    from .html_motion import RESOLUTIONS
    if resolution not in RESOLUTIONS:
        raise runctl.RunError("动画分辨率仅支持 1080p 或 4k")
    if music_mode not in ("auto", "continuous", "transition", "none"):
        raise runctl.RunError("未知的过场音乐模式")
    if bpm is not None and (not math.isfinite(bpm) or not 30 <= bpm <= 300):
        raise runctl.RunError("BPM 必须在 30 到 300 之间")
    with runctl.RunLease(run_id):
        exports.recover(runctl.run_dir(run_id))
        plan = load_plan(run_id)
        if not 0 <= after <= len(plan["timeline"]):
            raise runctl.RunError("插入位置超出时间线范围")
        revision = plan.get("revision", 0)
        if expected_revision is not None and expected_revision != revision:
            raise runctl.RunError("计划已被修改，请刷新后重试")
        from . import html_motion
        before = plan["timeline"][after - 1] if after else None
        following = plan["timeline"][after] if after < len(plan["timeline"]) else None
        if before is None or following is None:
            raise runctl.RunError("过场必须插入两段相邻视频之间")
        if before.get("kind") != "video" or following.get("kind") != "video":
            raise runctl.RunError("请选择两段相邻视频之间的位置")
        transition_start = sum(float(row.get("use_duration", 0)) for row in plan["timeline"][:after])
        transition_plan, documents = html_motion.generate_pages(
            prompt, duration, before, following, copy=str(plan.get("copy", "")), resolution=resolution,
            music=plan.get("music") if isinstance(plan.get("music"), dict) else {},
            music_mode=music_mode, bpm=bpm, transition_start=transition_start)
        paths = [html_motion.save(runctl.run_dir(run_id), document) for document in documents]
        pages = [{"path": str(path), "duration": scene["duration"], "brief": scene["brief"]}
                 for path, scene in zip(paths, transition_plan["scenes"], strict=True)]
        path = paths[0]
        width, height = RESOLUTIONS[resolution]
        row = {"seq": after + 1, "media": path.name, "kind": "html", "source": "generated",
               "local_path": str(path), "start_offset": 0, "use_duration": duration,
               "width": width, "height": height, "resolution": resolution,
               "html_pages": pages, "transition_plan": transition_plan,
               "music_sync": transition_plan.get("music_analysis", {}),
               "segment_text": "", "role": "过场", "intensity": 3,
               "note": transition_plan.get("concept") or prompt.strip() or "LLM 生成的 HTML 过场动画"}
        operation = {"op": "insert_generated", "after": after, "row_data": row}
        return _rebuild_locked(run_id, operation, expected_revision, preview=preview)


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
    elif kind == "insert_generated":
        after = operation.get("after")
        row = operation.get("row_data")
        if not isinstance(after, int) or isinstance(after, bool) or not 0 <= after <= len(rows):
            raise runctl.RunError("插入位置超出时间线范围")
        if not isinstance(row, dict) or row.get("source") != "generated" or row.get("kind") != "html":
            raise runctl.RunError("无效的 HTML 动画行")
        rows.insert(after, deepcopy(row))
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
