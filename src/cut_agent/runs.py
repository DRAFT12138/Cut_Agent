"""阶段驱动的运行控制（P8）：阶段编排 / 状态机 / 断电恢复 / 暂停恢复。

设计（CRAFT.md P8）：
- 退出 LangGraph 图驱动（节点保持纯函数 (state)->patch），按阶段表手动顺序执行；
- 阶段产物 stages/<名>.json 原子落盘，"有效 JSON 即该阶段完成"——
  断电判定不依赖 run.json 自身完整；
- 暂停/取消走 RunControl 协作标志：节点检查点抛 RunHalted，驱动捕获并持久化状态退出；
- 服务/CLI 启动时 recover_interrupted() 把 status=running 的 run 标记 interrupted
  并给出 resumable_from，resume_run() 从断点继续。

run 目录布局：
    output/runs/<run_id>/
      run.json            状态机 + 各阶段状态 + config 快照
      input.json          文案 + 素材目录 + seed + options
      log.jsonl           事件流 {seq, ts, stage, type: status|progress|log, ...}
      stages/<名>.json    阶段产物（= 节点 patch 去掉 log）
      （P2/P4/P9 追加：plan.json / plan.md / frames/ / web/ / preview.mp4 ...）
"""
from __future__ import annotations

import os
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import exports, graph
from . import runctl
from .checkpoints import media_snapshot
from .runctl import (EventLog, RunControl, RunError, RunHalted,
                     pid_alive, read_json, run_dir, runs_root, stage_path,
                     write_json_atomic)

# 阶段表：顺序 = 执行顺序 = 前端 Steps 顺序。
STAGES: list[tuple[str, Callable[[dict], dict]]] = [
    ("scan_media", graph.scan_media),
    ("plan_segments", graph.plan_segments),
    ("understand_media", graph.understand_media),
    ("build_timeline", graph.build_timeline),
    ("explore_web", graph.explore_web),
    ("pick_music", graph.pick_music),
    ("write_doc", graph.write_doc),
    ("finishing_guide", graph.finishing_guide),
]
STAGE_NAMES = [n for n, _ in STAGES]
_META_LOCK = threading.RLock()


@dataclass
class RunHandle:
    run_id: str
    control: RunControl
    log: EventLog
    thread: threading.Thread | None = field(default=None)
    lease: runctl.RunLease | None = field(default=None)

    def wait(self, timeout: float | None = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout)


# ---------------- 状态读写 ----------------

def _initial_state(media_dir: str, copy: str) -> dict:
    return {"media_folder": media_dir, "copy": copy,
            "log": [], "media": [], "segments": [],
            "timeline": [], "web_assets": [], "music": {}}


def rebuild_state(run_id: str) -> dict:
    """从 input.json + 已有阶段产物重建 state（断电恢复与续跑的入口）。"""
    inp = read_json(run_dir(run_id) / "input.json")
    if inp is None:
        raise RunError(f"run {run_id} 缺少 input.json，无法重建")
    state = _initial_state(inp["media_dir"], inp["copy"])
    for name, _ in STAGES:
        art = read_json(stage_path(run_id, name))
        if not isinstance(art, dict):
            break
        state.update({k: v for k, v in art.items() if k != "_stage"})
    return state


def first_incomplete(run_id: str) -> int:
    """第一个没有产物的阶段下标；全部完成返回 len(STAGES)。"""
    for i, (name, _) in enumerate(STAGES):
        if not isinstance(read_json(stage_path(run_id, name)), dict):
            return i
    return len(STAGES)


def _next_stage_name(run_id: str) -> str | None:
    i = first_incomplete(run_id)
    return STAGE_NAMES[i] if i < len(STAGES) else None


def _recovered_stages(run_id: str) -> list[dict]:
    complete = first_incomplete(run_id)
    entries = []
    for i, name in enumerate(STAGE_NAMES):
        info = (read_json(stage_path(run_id, name)) or {}).get("_stage", {}) if i < complete else {}
        info = info if isinstance(info, dict) else {}
        entries.append({"name": name, "status": info.get("status", "done") if i < complete else "pending",
                        "warnings": info.get("warnings", []),
                        **{k: info[k] for k in ("progress", "elapsed") if k in info}})
    return entries


def _invalidate_suffix(run_id: str, start_idx: int) -> None:
    """Archive the entire invalid suffix before filling its first gap.

    If interrupted during archiving, the gap is still present, so the next
    resume repeats this step before executing any node. Never delete evidence.
    Caller must own the worker lease.
    """
    root = run_dir(run_id).resolve()
    archive = root / "history" / "checkpoints" / str(time.time_ns())
    for name in STAGE_NAMES[start_idx:]:
        source = stage_path(run_id, name).resolve()
        target = (archive / source.name).resolve()
        if not source.is_relative_to(root) or not target.is_relative_to(root):
            raise RunError("断点归档路径超出任务目录")
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)


def _run_json_path(run_id: str) -> Path:
    return run_dir(run_id) / "run.json"


def _valid_metadata(meta, run_id: str) -> bool:
    return (isinstance(meta, dict) and meta.get("id") == run_id
            and isinstance(meta.get("stages"), list)
            and all(isinstance(s, dict) and isinstance(s.get("name"), str)
                    for s in meta["stages"]))


def _update_run_json(run_id: str, **kw) -> dict:
    with _META_LOCK:
        meta = read_json(_run_json_path(run_id))
        if not isinstance(meta, dict):
            meta = {"id": run_id, "created": time.time(), "stages": []}
        meta["updated"] = time.time()
        meta.update(kw)
        meta["id"] = run_id
        meta.setdefault("stages", [])
        write_json_atomic(_run_json_path(run_id), meta)
        return meta


def _stage_status(run_id: str, name: str, status: str, **kw) -> None:
    with _META_LOCK:
        meta = read_json(_run_json_path(run_id))
        if not _valid_metadata(meta, run_id):
            meta = {"id": run_id, "stages": []}
        entry = next((s for s in meta.get("stages", []) if s.get("name") == name), None)
        if entry is None:
            entry = {"name": name}
            meta.setdefault("stages", []).append(entry)
        entry["status"] = status
        entry.update(kw)
        meta["updated"] = time.time()
        write_json_atomic(_run_json_path(run_id), meta)


# ---------------- 驱动 ----------------

def _seed_of(run_id: str) -> int | None:
    inp = read_json(run_dir(run_id) / "input.json") or {}
    s = inp.get("seed")
    return int(s) if isinstance(s, (int, float)) else None


def _worker_log(run_id: str) -> EventLog:
    return EventLog(run_id, on_progress=lambda name, progress:
                    _stage_status(run_id, name, "running", progress=progress))


def _drive(run_id: str, control: RunControl, log: EventLog,
           start_idx: int, state: dict) -> None:
    """Guard node execution and persistence together; always release via caller."""
    name = STAGE_NAMES[min(start_idx, len(STAGES) - 1)]
    try:
        rd = run_dir(run_id)
        ctx = graph.RunCtx(run_id=run_id, control=control, log=log,
                           run_dir=rd, seed=_seed_of(run_id),
                           options=(read_json(rd / "input.json") or {}).get("options", {}))
        for i, (name, fn) in enumerate(STAGES):
            if i < start_idx:
                continue
            log.event(name, "status", what="start")
            _stage_status(run_id, name, "running", started_at=time.time(),
                          progress={"done": 0, "total": 1}, error=None)
            state["_ctx"] = ctx
            ctx.warnings.clear()
            log_before = len(state.get("log", []))
            t0 = time.time()
            runctl.check_stop(control, name, log)
            try:
                with runctl.control_scope(control):
                    patch = fn(state)
            finally:
                state.pop("_ctx", None)
            if not isinstance(patch, dict):
                raise TypeError(f"阶段 {name} 必须返回对象")
            for line in patch.get("log", [])[log_before:]:
                log.event(name, "log", msg=str(line))
            art = {k: v for k, v in patch.items() if k != "log"}
            result_status = "degraded" if ctx.warnings else "done"
            progress = log.progress.get(name, {"done": 0, "total": 1}).copy()
            progress["done"] = progress["total"]
            info = {"status": result_status, "warnings": list(ctx.warnings),
                    "progress": progress, "elapsed": round(time.time() - t0, 1)}
            art["_stage"] = info
            write_json_atomic(stage_path(run_id, name), art)
            state.update(patch)
            _stage_status(run_id, name, **info, finished_at=time.time(), error=None)
        _update_run_json(run_id, status="done", resumable_from=None, error=None,
                         pid=None, control_requested=None)
        log.event(STAGE_NAMES[-1], "status", what="run-done")
    except RunHalted:
        try:
            _halt(run_id, control, log, name)
        except Exception as exc:
            _record_failure(run_id, name, log, exc)
    except Exception as exc:
        _record_failure(run_id, name, log, exc)
    finally:
        state.pop("_ctx", None)


def _record_failure(run_id: str, name: str, log: EventLog, exc: Exception) -> None:
    """A valid committed artifact survives even if subsequent metadata fails."""
    error = f"{name}: {type(exc).__name__}: {str(exc)[:300]}"
    try:
        entries = _recovered_stages(run_id)
        entry = next((s for s in entries if s["name"] == name), None)
        if entry and entry["status"] == "pending":
            entry.update(status="failed", error=error, finished_at=time.time(),
                         progress=log.progress.get(name, {"done": 0, "total": 1}))
        _update_run_json(run_id, status="failed", stages=entries,
                         resumable_from=_next_stage_name(run_id), error=error,
                         pid=None, control_requested=None)
    except Exception:
        # Disk unavailable: the lease will still be released; a later recovery
        # scan uses ownership and checkpoints even while this PID remains alive.
        logging.getLogger(__name__).exception("无法保存任务失败状态 %s", run_id)
    try:
        log.event(name, "status", what="failed", error=error)
    except Exception:
        logging.getLogger(__name__).exception("无法保存任务失败事件 %s", run_id)


def _halt(run_id: str, control: RunControl, log: EventLog, stage: str) -> None:
    kind = "canceled" if control.canceled else "paused"
    # 阶段本身是"未完成/停在途中"，不是失败；原因由 run 级 status 表达
    _stage_status(run_id, stage, "paused")
    _update_run_json(run_id, status=kind, resumable_from=stage,
                     pid=None, control_requested=None,
                     error=("已取消（产物保留，可恢复）" if kind == "canceled"
                            else "已暂停，可恢复"))
    log.event(stage, "status", what=f"run-{kind}")


# ---------------- 对外操作 ----------------

def start_run(media_dir: str, copy: str, *, seed: int | None = None,
              options: dict | None = None, sync: bool = False) -> tuple[RunHandle, dict]:
    """新建 run。sync=True 阻塞到结束（CLI），False 后台线程（Web）。返回 (handle, state)。"""
    media_dir = str(Path(media_dir).resolve())
    run_id = runctl.new_run_id()
    rd = run_dir(run_id)
    control = RunControl(run_id)
    # Own the directory before publishing input/pending state to recovery scans.
    lease = runctl.RunLease(run_id)
    try:
        log = _worker_log(run_id)
        write_json_atomic(rd / "input.json",
                          {"media_dir": str(media_dir), "copy": copy,
                           "seed": seed, "options": options or {},
                           "media_snapshot": media_snapshot(media_dir),
                           "created": time.time()})
        _update_run_json(run_id, status="pending", worker_lock_version=1,
                         stages=[{"name": n, "status": "pending"} for n in STAGE_NAMES],
                         config={"media_dir": str(media_dir), "seed": seed,
                                 "options": options or {}})
        state = rebuild_state(run_id)
        handle = RunHandle(run_id, control, log, lease=lease)
        _update_run_json(run_id, status="running", pid=os.getpid())
        if sync:
            _drive_and_release(handle, 0, state)
        else:
            register_active(handle)
            handle.thread = runctl.run_in_thread(
                control, lambda: _drive_and_release(handle, 0, state))
        return handle, state
    except BaseException:
        unregister_active(run_id)
        lease.close()
        raise


def _drive_and_release(handle: RunHandle, start_idx: int, state: dict) -> None:
    """后台驱动：跑完后注销活动句柄。"""
    try:
        _drive(handle.run_id, handle.control, handle.log, start_idx, state)
    finally:
        if _ACTIVE.get(handle.run_id) is handle.control:
            unregister_active(handle.run_id)
        if handle.lease:
            handle.lease.close()


def resume_run(run_id: str) -> RunHandle:
    """从断点恢复 paused/interrupted/failed/canceled 的 run。"""
    if not (run_dir(run_id) / "input.json").is_file():
        raise RunError(f"run 不存在: {run_id}")
    lease = runctl.RunLease(run_id)
    try:
        return _resume_locked(run_id, lease)
    except BaseException:
        lease.close()
        raise


def _resume_locked(run_id: str, lease: runctl.RunLease) -> RunHandle:
    rd = run_dir(run_id)
    recovered_export = exports.recover(rd)
    meta = read_json(rd / "run.json")
    if not isinstance(meta, dict):
        raise RunError(f"run 不存在: {run_id}")
    if recovered_export:
        lease.close()
        return RunHandle(run_id, RunControl(run_id), _worker_log(run_id))
    incomplete = first_incomplete(run_id) < len(STAGES)
    if meta.get("status") not in ("paused", "interrupted", "failed", "canceled") and not (
            meta.get("status") == "done" and incomplete):
        raise RunError(f"run {run_id} 状态为 {meta.get('status')}，不可恢复")
    inp = read_json(rd / "input.json") or {}
    if "media_snapshot" in inp and media_snapshot(inp["media_dir"]) != inp["media_snapshot"]:
        raise RunError("素材已新增、删除或修改，无法安全复用断点；请新建任务")
    plan = read_json(rd / "plan.json")
    if incomplete and isinstance(plan, dict) and isinstance(plan.get("revision"), int) and plan["revision"] > 0:
        # A saved edit owns the full state. Repair its exports/checkpoints
        # offline instead of running old matching/search snapshots over it.
        from .editing import _rebuild_locked
        _rebuild_locked(run_id)
        lease.close()
        return RunHandle(run_id, RunControl(run_id), _worker_log(run_id))
    control = RunControl(run_id)  # 新标志（清除旧的 stop）
    log = _worker_log(run_id)
    start_idx = first_incomplete(run_id)
    _invalidate_suffix(run_id, start_idx)
    state = rebuild_state(run_id)
    _update_run_json(run_id, status="running",
                     resumable_from=_next_stage_name(run_id), error=None,
                     pid=os.getpid(), worker_lock_version=1, control_requested=None,
                     stages=_recovered_stages(run_id))
    handle = RunHandle(run_id, control, log)
    handle.lease = lease
    register_active(handle)
    handle.thread = runctl.run_in_thread(
        control, lambda: _drive_and_release(handle, start_idx, state))
    return handle


def pause_run(run_id: str) -> None:
    """暂停：协作式，在飞子任务跑完当前单位后停。"""
    meta = read_json(_run_json_path(run_id))
    if meta is None:
        raise RunError(f"run 不存在: {run_id}")
    if meta.get("status") != "running":
        raise RunError(f"run {run_id} 不在运行中（{meta.get('status')}），无法暂停")
    # 找到在跑的控制（CLI sync 模式无句柄，这里只处理 Web/后台句柄表）
    ctrl = _ACTIVE.get(run_id)
    if ctrl is None:
        raise RunError(f"run {run_id} 不在本进程运行（可能是其他进程或 CLI 同步运行）")
    _update_run_json(run_id, control_requested="pause")
    ctrl.request_pause()


def start_variant(run_id: str, seed: int | None = None, *, sync: bool = False):
    """New independent run with the same copy/media snapshot and a recorded A/B group."""
    inp = read_json(run_dir(run_id) / "input.json")
    if not isinstance(inp, dict):
        raise RunError("原任务不存在")
    if "media_snapshot" in inp and media_snapshot(inp["media_dir"]) != inp["media_snapshot"]:
        raise RunError("原任务素材已变化，不能作为同输入 A/B；请新建任务")
    options = dict(inp.get("options") or {})
    options.update(parent_run=run_id, comparison_group=options.get("comparison_group", run_id))
    if seed is None:
        seed = (inp.get("seed") or 0) + 1
    return start_run(inp["media_dir"], inp["copy"], seed=seed, options=options, sync=sync)


def cancel_run(run_id: str) -> None:
    meta = read_json(_run_json_path(run_id))
    if meta is None:
        raise RunError(f"run 不存在: {run_id}")
    if meta.get("status") != "running":
        raise RunError(f"run {run_id} 不在运行中（{meta.get('status')}），无法取消")
    ctrl = _ACTIVE.get(run_id)
    if ctrl is None:
        raise RunError(f"run {run_id} 不在本进程运行（可能是其他进程或 CLI 同步运行）")
    _update_run_json(run_id, control_requested="cancel")
    ctrl.request_cancel()


# 本进程内活动 run 的控制句柄（Web 进程内 pause/cancel 用）
_ACTIVE: dict[str, RunControl] = {}


def register_active(handle: RunHandle) -> None:
    _ACTIVE[handle.run_id] = handle.control


def unregister_active(run_id: str) -> None:
    _ACTIVE.pop(run_id, None)


def recover_interrupted(run_id: str | None = None) -> list[str]:
    """Recover unowned work from its contiguous checkpoints.

    Modern workers always hold a lease, including initialization. Only legacy
    metadata needs the extra PID check. A surviving Web process is not proof
    that its worker thread still exists. Intentionally paused runs stay paused.
    """
    out: list[str] = []
    root = runs_root()
    if not root.is_dir():
        return out
    for d in ([run_dir(run_id)] if run_id is not None else sorted(root.iterdir())):
        if not d.is_dir():
            continue
        try:
            lease = runctl.RunLease(d.name)
        except RunError:
            continue
        try:
            if exports.pending(d):
                try:
                    exports.recover(d)
                    out.append(d.name)
                except (OSError, RunError) as exc:
                    # Keep the read barrier until the payload can be replayed.
                    # Status remains available; plan/stage/file reads are gated.
                    try:
                        _update_run_json(d.name, export_error=str(exc)[:300])
                    except OSError:
                        pass
                    continue
            meta = read_json(d / "run.json")
            if not _valid_metadata(meta, d.name):
                inp = read_json(d / "input.json")
                if not isinstance(inp, dict) or not {"media_dir", "copy"} <= inp.keys():
                    continue
                rid = d.name
                idx = first_incomplete(rid)
                _update_run_json(rid, status="interrupted" if idx < len(STAGES) else "done",
                                 created=inp.get("created", d.stat().st_mtime),
                                 resumable_from=_next_stage_name(rid), pid=None,
                                 stages=_recovered_stages(rid), worker_lock_version=1)
                out.append(rid)
                continue
            if not meta or not meta.get("id"):
                continue
            status = meta.get("status")
            unowned = status in ("running", "pending") and (
                meta.get("worker_lock_version") == 1 or not pid_alive(meta.get("pid")))
            incomplete = first_incomplete(d.name) < len(STAGES)
            if unowned or (status == "done" and incomplete):
                rid = d.name
                recovered = "interrupted" if incomplete else "done"
                _update_run_json(rid, status=recovered,
                                 stages=_recovered_stages(rid),
                                 resumable_from=_next_stage_name(rid),
                                 error="执行器退出或断点不完整，可从断点恢复" if incomplete else None,
                                 pid=None, control_requested=None, worker_lock_version=1)
                try:
                    EventLog(rid).event(_next_stage_name(rid) or "?",
                                        "status", what=f"recovered-{recovered}")
                except Exception:
                    pass
                out.append(rid)
        finally:
            lease.close()
    return out


# ---------------- 查询 ----------------

def status_of(run_id: str) -> dict:
    recover_interrupted(run_id)
    meta = read_json(_run_json_path(run_id))
    if not _valid_metadata(meta, run_id):
        raise RunError(f"run 不存在: {run_id}")
    meta["resumable_from"] = meta.get("resumable_from") or _next_stage_name(run_id)
    meta["stages_done"] = sum(1 for s in meta.get("stages", [])
                              if s.get("status") in ("done", "degraded"))
    if exports.pending(run_dir(run_id)):
        detail = meta.get("export_error")
        meta.update(export_pending=True, status="recovering",
                    error="编辑导出尚未提交，文件恢复完成后可继续读取" + (f"：{detail}" if detail else ""))
    return meta


def stage_artifact(run_id: str, name: str):
    if name not in STAGE_NAMES:
        raise RunError(f"未知阶段: {name}")
    if exports.pending(run_dir(run_id)):
        recover_interrupted(run_id)
        if exports.pending(run_dir(run_id)):
            return None
    if STAGE_NAMES.index(name) >= first_incomplete(run_id):
        return None
    return read_json(stage_path(run_id, name))


def list_runs() -> list[dict]:
    recover_interrupted()
    root = runs_root()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta = read_json(d / "run.json")
        if not _valid_metadata(meta, d.name):
            continue
        inp = read_json(d / "input.json") or {}
        out.append({
            "id": meta["id"],
            "status": "recovering" if exports.pending(d) else meta.get("status"),
            "created": meta.get("created"),
            "stages_done": sum(1 for s in meta.get("stages", [])
                               if s.get("status") in ("done", "degraded")),
            "stages_total": len(STAGE_NAMES),
            "copy_head": str(inp.get("copy", ""))[:60].replace("\n", " "),
            "seed": inp.get("seed"),
            "comparison_group": (inp.get("options") or {}).get("comparison_group", meta["id"]),
            "resumable_from": meta.get("resumable_from") or _next_stage_name(meta["id"]),
        })
    out.sort(key=lambda r: r.get("created") or 0, reverse=True)
    return out
