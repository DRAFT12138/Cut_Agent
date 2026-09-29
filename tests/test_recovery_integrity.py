"""Fault injection at the boundaries that determine safe restart behavior."""
import os
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cut_agent import cli, graph, runctl, runs


def test_atomic_json_write_retries_short_replace_denial(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    runctl.write_json_atomic(path, {"value": "old"})
    replace = os.replace
    attempts = 0

    def briefly_denied(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("destination briefly held by a reader")
        replace(source, target)

    monkeypatch.setattr(runctl.os, "replace", briefly_denied)
    runctl.write_json_atomic(path, {"value": "new"})
    assert attempts == 3
    assert runctl.read_json(path) == {"value": "new"}
    assert not list(tmp_path.glob(".run.json.*.tmp"))


def test_atomic_json_write_keeps_old_file_after_persistent_replace_denial(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    runctl.write_json_atomic(path, {"value": "old"})
    attempts = 0

    def denied(_source, _target):
        nonlocal attempts
        attempts += 1
        raise PermissionError("destination remains locked")

    monkeypatch.setattr(runctl.os, "replace", denied)
    with pytest.raises(PermissionError):
        runctl.write_json_atomic(path, {"value": "new"})
    assert attempts == 5
    assert runctl.read_json(path) == {"value": "old"}
    assert not list(tmp_path.glob(".run.json.*.tmp"))


def test_json_reader_retries_short_access_denial(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    runctl.write_json_atomic(path, {"status": "running"})
    real_open = open
    attempts = 0

    def briefly_denied(source, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("writer briefly holds destination")
        return real_open(source, *args, **kwargs)

    monkeypatch.setattr(runctl, "open", briefly_denied, raising=False)
    assert runctl.read_json(path) == {"status": "running"}
    assert attempts == 3


def stages(monkeypatch, pairs):
    monkeypatch.setattr(runs, "STAGES", pairs)
    monkeypatch.setattr(runs, "STAGE_NAMES", [name for name, _ in pairs])


def partial(rid, status="interrupted", **meta):
    root = runctl.run_dir(rid)
    runctl.write_json_atomic(root / "input.json", {"media_dir": "unused", "copy": "copy"})
    runctl.write_json_atomic(root / "run.json", {
        "id": rid, "status": status, "worker_lock_version": 1,
        "pid": os.getpid(), "stages": [], **meta})
    return root


def test_gap_then_second_pause_never_reuses_old_suffix(tmp_runs, monkeypatch):
    calls = []

    def second(state):
        calls.append("second")
        state["_ctx"].control.request_pause()
        return {"value": "fresh"}

    def third(state):
        calls.append("third")
        assert state["value"] == "fresh"
        return {"result": "fresh final"}

    stages(monkeypatch, [("first", lambda s: pytest.fail("first is complete")),
                         ("second", second), ("third", third)])
    root = partial("gap-run")
    runctl.write_json_atomic(root / "stages/first.json", {"value": "initial"})
    runctl.write_json_atomic(root / "stages/third.json", {"result": "old final"})
    assert runs.stage_artifact("gap-run", "third") is None
    runs.resume_run("gap-run").wait(5)
    assert runs.status_of("gap-run")["status"] == "paused"
    assert runs.first_incomplete("gap-run") == 2
    archived = list((root / "history/checkpoints").glob("*/third.json"))
    assert len(archived) == 1
    assert runctl.read_json(archived[0])["result"] == "old final"
    runs.resume_run("gap-run").wait(5)
    assert calls == ["second", "third"]
    assert runs.rebuild_state("gap-run")["result"] == "fresh final"
    assert runs.status_of("gap-run")["status"] == "done"


@pytest.mark.parametrize("boundary,committed", [
    ("artifact", False), ("stage-status", True), ("log", False), ("run-status", True)])
def test_persistence_failure_resumes_from_committed_boundary(tmp_runs, monkeypatch, boundary, committed):
    calls = []
    stages(monkeypatch, [("first", lambda s: calls.append(1) or {"value": "saved", "log": ["node returned"]})])
    write, event = runs.write_json_atomic, runctl.EventLog.event
    injected = []

    def fail_write(path, obj):
        hit = (boundary == "artifact" and path.name == "first.json") or (
            boundary == "stage-status" and path.name == "run.json" and
            any(s.get("status") == "done" for s in obj.get("stages", []))) or (
            boundary == "run-status" and path.name == "run.json" and obj.get("status") == "done")
        if hit and not injected:
            injected.append(True)
            raise OSError("simulated persistence failure")
        return write(path, obj)

    def fail_event(self, stage, kind, **kw):
        if boundary == "log" and kind == "log" and not injected:
            injected.append(True)
            raise OSError("simulated log failure")
        return event(self, stage, kind, **kw)

    monkeypatch.setattr(runs, "write_json_atomic", fail_write)
    monkeypatch.setattr(runctl.EventLog, "event", fail_event)
    handle, state = runs.start_run("unused", "copy", sync=True)
    meta = runs.status_of(handle.run_id)
    assert injected and meta["status"] == "failed"
    assert "simulated" in meta["error"]
    assert runs.first_incomplete(handle.run_id) == int(committed)
    assert "_ctx" not in state and handle.run_id not in runs._ACTIVE
    with runctl.RunLease(handle.run_id):
        pass
    runs.resume_run(handle.run_id).wait(5)
    assert calls == [1] * (1 if committed else 2)
    assert runs.status_of(handle.run_id)["status"] == "done"


def test_total_write_failure_recovers_without_killing_service(tmp_runs, monkeypatch):
    unavailable = False
    write = runs.write_json_atomic

    def node(state):
        nonlocal unavailable
        unavailable = True
        return {"value": "not yet saved"}

    def disk_failure(path, obj):
        if unavailable:
            raise OSError("disk unavailable")
        return write(path, obj)

    stages(monkeypatch, [("first", node)])
    monkeypatch.setattr(runs, "write_json_atomic", disk_failure)
    handle, _ = runs.start_run("unused", "copy", sync=True)
    root = runctl.run_dir(handle.run_id)
    assert runctl.read_json(root / "run.json")["status"] == "running"
    assert runctl.read_json(root / "run.json")["pid"] == os.getpid()
    unavailable = False
    # The same service process is alive; the now-unowned lease proves the worker stopped.
    meta = runs.status_of(handle.run_id)
    assert meta["status"] == "interrupted" and meta["resumable_from"] == "first"
    stages(monkeypatch, [("first", lambda s: {"value": "saved after recovery"})])
    runs.resume_run(handle.run_id).wait(5)
    assert runs.status_of(handle.run_id)["status"] == "done"


def test_progress_survives_pause_and_metadata_reconstruction(tmp_runs, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    attempts = []

    def node(state):
        attempts.append(1)
        graph._emit(state, "first", "progress", done=1, total=3, item="one.mp4")
        if len(attempts) == 1:
            entered.set()
            assert release.wait(5)
            graph._checkpoint(state, "first")
        return {"value": "complete"}

    stages(monkeypatch, [("first", node)])
    handle, _ = runs.start_run("unused", "copy")
    try:
        assert entered.wait(5)
        meta = runs.status_of(handle.run_id)
        assert meta["status"] == "running"  # recovery must leave a live lease alone
        assert meta["stages"][0]["progress"] == {"done": 1, "total": 3, "item": "one.mp4"}
        runs.pause_run(handle.run_id)
        assert runs.status_of(handle.run_id)["control_requested"] == "pause"
    finally:
        release.set()
        handle.wait(5)
    meta = runs.status_of(handle.run_id)
    assert meta["status"] == "paused" and meta["stages"][0]["progress"]["done"] == 1
    runs.resume_run(handle.run_id).wait(5)
    (runctl.run_dir(handle.run_id) / "run.json").write_bytes(b"\xff\xfe{broken")
    recovered = runs.status_of(handle.run_id)
    assert recovered["status"] == "done"
    assert recovered["stages"][0]["progress"] == {"done": 3, "total": 3, "item": "one.mp4"}


@pytest.mark.parametrize("status", ["pending", "running"])
def test_unowned_modern_run_recovers_with_live_pid(tmp_runs, status):
    partial("orphan", status=status)
    assert runs.status_of("orphan")["status"] == "interrupted"


def test_legacy_live_worker_is_not_claimed(tmp_runs):
    partial("legacy", status="running", worker_lock_version=None)
    assert runs.status_of("legacy")["status"] == "running"


def test_done_run_with_damaged_checkpoint_becomes_resumable(tmp_runs):
    root = partial("damaged", status="done")
    runctl.write_json_atomic(root / "stages/scan_media.json", {"media": []})
    runctl.write_json_atomic(root / "stages/understand_media.json", {"media": ["stale"]})
    meta = runs.status_of("damaged")
    assert meta["status"] == "interrupted"
    assert meta["resumable_from"] == "plan_segments"
    assert meta["stages_done"] == 1


def test_archive_interruption_keeps_gap_before_any_node_runs(tmp_runs, monkeypatch):
    calls = []
    stages(monkeypatch, [(name, lambda s: calls.append(1) or {}) for name in ("first", "second", "third")])
    root = partial("archive-run")
    (root / "stages").mkdir()
    (root / "stages/first.json").write_text("{", encoding="utf-8")
    runctl.write_json_atomic(root / "stages/second.json", {"old": 2})
    runctl.write_json_atomic(root / "stages/third.json", {"old": 3})
    replace = Path.replace
    interrupted = []
    def fail_once(path, target):
        if path.name == "second.json" and not interrupted:
            interrupted.append(1)
            raise OSError("interrupted archival")
        return replace(path, target)
    monkeypatch.setattr(Path, "replace", fail_once)
    with pytest.raises(OSError, match="archival"):
        runs.resume_run("archive-run")
    assert not calls and runs.first_incomplete("archive-run") == 0
    runs.resume_run("archive-run").wait(5)
    assert len(calls) == 3
    assert "old" not in runs.rebuild_state("archive-run")
    assert len(list((root / "history/checkpoints").glob("*/*.json"))) == 3


def test_partial_utf8_log_can_be_read_and_extended(tmp_runs):
    root = partial("partial-log")
    path = root / "log.jsonl"
    path.write_bytes(b'{"seq":1,"stage":"first","type":"status"}\n[]\n{"seq":2,"msg":"\xe4\xb8')
    log = runctl.EventLog("partial-log")
    assert log.latest == 1
    log.event("first", "log", msg="recovered")
    assert [r["seq"] for r in log.tail()] == [1, 2]
    assert cli._events_since("partial-log", 1)[0]["msg"] == "recovered"


def test_killed_interpreter_resumes_in_another_process_context(tmp_runs, monkeypatch):
    code = '''
import os, json, sys, threading
from pathlib import Path
sys.path[:] = PATHS
from cut_agent import graph, runctl, runs
runctl.set_runs_root_for_test(Path(sys.argv[1]))
def second(state):
    graph._emit(state, 'second', 'progress', done=1, total=2)
    print(json.dumps({'pid': os.getpid(), 'run_id': state['_ctx'].run_id}), flush=True)
    threading.Event().wait(30)
    return {'value': 'should not finish'}
runs.STAGES = [('first', lambda state: {'value': 'checkpoint saved'}), ('second', second)]
runs.STAGE_NAMES = ['first', 'second']
runs.start_run('unused', 'copy', sync=True)
'''.replace("PATHS", repr(sys.path))
    stages(monkeypatch, [("first", lambda s: pytest.fail("committed stage must not execute again")),
                         ("second", lambda s: {"reused_value": s["value"]})])
    child = subprocess.Popen([sys._base_executable, "-c", code, str(tmp_runs)], stdout=subprocess.PIPE, text=True)
    try:
        report = json.loads(child.stdout.readline())
        assert report["pid"] == child.pid
        rid = report["run_id"]
        assert runs.status_of(rid)["status"] == "running"
    finally:
        child.terminate()
        child.wait(timeout=5)
        child.stdout.close()
    assert runs.status_of(rid)["status"] == "interrupted"
    assert runs.status_of(rid)["resumable_from"] == "second"
    runs.resume_run(rid).wait(5)
    assert runs.status_of(rid)["status"] == "done"
    assert runs.rebuild_state(rid)["reused_value"] == "checkpoint saved"


def test_cli_follow_exits_on_interrupted_run(tmp_runs, monkeypatch):
    partial("follow-orphan", status="running")
    monkeypatch.setattr(cli.time, "sleep", lambda s: pytest.fail("interrupted run must not loop"))
    assert cli._follow("follow-orphan", until_done=True) == 0


@pytest.mark.parametrize("metadata", [[], {}, {"id": "wrong", "stages": []},
                                      {"id": "bad-shape", "stages": [None]}])
def test_valid_json_with_corrupt_metadata_shape_is_rebuilt(tmp_runs, metadata):
    root = partial("bad-shape")
    runctl.write_json_atomic(root / "run.json", metadata)
    runctl.write_json_atomic(root / "stages/scan_media.json", {"media": []})
    meta = runs.status_of("bad-shape")
    assert meta["id"] == "bad-shape" and meta["status"] == "interrupted"
    assert meta["stages_done"] == 1 and meta["resumable_from"] == "plan_segments"
