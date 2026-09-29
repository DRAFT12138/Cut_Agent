"""Publication failures must never expose or resume a mixture of edit revisions."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from cut_agent import exports, graph, runctl, runs
from cut_agent.editing import load_plan, rebuild
from cut_agent.server import make_app


@pytest.fixture
def completed(tmp_runs, fake_llm, monkeypatch):
    handle, _ = runs.start_run("unused", "one two three", sync=True)
    assert runs.status_of(handle.run_id)["status"] == "done"
    monkeypatch.setattr(graph, "chat_json", lambda *a, **kw: pytest.fail("edit recovery must be offline"))
    return handle.run_id


def assert_consistent(rid, revision):
    root = runctl.run_dir(rid)
    plan = load_plan(rid)
    assert not exports.pending(root)
    assert plan["revision"] == revision
    assert runs.status_of(rid)["status"] == "done"
    assert runs.first_incomplete(rid) == 8
    for name in ("build_timeline", "explore_web", "write_doc"):
        artifact = runs.stage_artifact(rid, name)
        assert artifact["timeline"] == plan["timeline"]
        assert artifact["critique"] == plan["critique"]
        assert artifact["_stage"]["edited_revision"] == revision
    assert runs.rebuild_state(rid)["timeline"] == plan["timeline"]
    assert runs.stage_artifact(rid, "finishing_guide")["finishing"] == plan["finishing"]
    assert (root / "plan.md").read_bytes() == (root / "粗剪方案.md").read_bytes()
    for row, card, step in zip(plan["timeline"], plan["storyboard"]["cards"], plan["finishing"]["steps"]):
        assert row["use_duration"] == card["picture_duration"]
        assert step["anchor"] == card
        assert (root / card["frame"]).is_file()
    return plan


@pytest.mark.parametrize("boundary", ["plan.md", "stages/explore_web.json", "plan.json", "run.json"])
def test_interrupted_publication_is_gated_and_replayed(completed, monkeypatch, boundary):
    root = runctl.run_dir(completed).resolve()
    copy = exports._copy_atomic
    injected = []
    def stop_after_replace(source, target):
        copy(source, target)
        if target.relative_to(root).as_posix() == boundary and not injected:
            injected.append(True)
            raise OSError("publication interrupted")
    monkeypatch.setattr(exports, "_copy_atomic", stop_after_replace)
    with pytest.raises(OSError, match="publication"):
        rebuild(completed, {"op": "duration", "row": 1, "seconds": 1})
    assert exports.pending(root)
    with runctl.RunLease(completed), TestClient(make_app()) as client:
        meta = client.get(f"/api/runs/{completed}").json()
        assert meta["export_pending"] and meta["has_artifacts"] == {}
        assert client.get(f"/api/runs/{completed}/plan").status_code == 404
        assert client.get(f"/api/runs/{completed}/stages/write_doc").status_code == 404
        assert client.get("/api/file", params={"path": str(root / "plan.md")}).status_code == 503
    # Reads recover the transaction under a lease; explicit resume also works
    # when the previous run status still said done.
    if boundary == "plan.json":
        runs.resume_run(completed).wait(5)
    else:
        runs.status_of(completed)
    assert assert_consistent(completed, 1)["timeline"][0]["use_duration"] == 1
    assert not list(root.glob(".rebuild-*"))


@pytest.mark.parametrize("gap", ["scan_media", "explore_web", "write_doc", "finishing_guide"])
def test_resume_with_checkpoint_gap_keeps_edited_plan_authoritative(completed, monkeypatch, gap):
    root = runctl.run_dir(completed)
    original = load_plan(completed)
    edited = rebuild(completed, {"op": "move", "row": 1, "to": 3})
    (root / "stages" / f"{gap}.json").write_text("{", encoding="utf-8")
    if gap != "explore_web":
        runctl.write_json_atomic(root / "stages/explore_web.json", {"timeline": original["timeline"]})
    monkeypatch.setattr(runs, "STAGES", [(name, lambda s: pytest.fail("saved edits must not rerun generation"))
                                         for name in runs.STAGE_NAMES])
    runs.resume_run(completed).wait(5)
    assert assert_consistent(completed, 2)["timeline"] == edited["timeline"]


def test_manifest_write_failure_keeps_original_exports(completed, monkeypatch):
    root = runctl.run_dir(completed)
    before = {name: (root / name).read_bytes() for name in ("plan.json", "plan.md", "run.json")}
    write = runctl.write_json_atomic
    def reject_manifest(path, content):
        if path.name == exports.MANIFEST:
            raise OSError("manifest unavailable")
        write(path, content)
    monkeypatch.setattr(runctl, "write_json_atomic", reject_manifest)
    with pytest.raises(OSError, match="manifest"):
        rebuild(completed, {"op": "drop", "row": 1})
    assert before == {name: (root / name).read_bytes() for name in before}
    assert not exports.pending(root) and not list(root.glob(".rebuild-*"))


def test_corrupt_payload_stays_gated_until_repaired(completed, monkeypatch):
    root = runctl.run_dir(completed)
    copy = exports._copy_atomic
    monkeypatch.setattr(exports, "_copy_atomic", lambda *a: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError):
        rebuild(completed, {"op": "drop", "row": 1})
    monkeypatch.setattr(exports, "_copy_atomic", copy)
    record = runctl.read_json(root / exports.MANIFEST)
    source = root / record["staging"] / "plan.md"
    valid = source.read_bytes()
    source.write_bytes(b"corrupt")
    before = (root / "plan.json").read_bytes()
    with runctl.RunLease(completed), pytest.raises(runctl.RunError, match="plan.md"):
        exports.recover(root)
    assert runs.status_of(completed)["export_pending"]
    assert (root / "plan.json").read_bytes() == before
    source.write_bytes(valid)
    runs.status_of(completed)
    assert_consistent(completed, 1)


@pytest.mark.parametrize("damage", ["missing_stage", "missing_frame", "duplicate", "outside", "revision"])
def test_invalid_manifest_cannot_publish_partial_revision(completed, monkeypatch, damage):
    from copy import deepcopy
    root = runctl.run_dir(completed)
    copy = exports._copy_atomic
    monkeypatch.setattr(exports, "_copy_atomic", lambda *a: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError):
        rebuild(completed, {"op": "drop", "row": 1})
    monkeypatch.setattr(exports, "_copy_atomic", copy)
    valid = runctl.read_json(root / exports.MANIFEST)
    damaged = deepcopy(valid)
    if damage.startswith("missing"):
        victim = "stages/explore_web.json" if damage == "missing_stage" else "frames/storyboard_0001.png"
        damaged["files"] = [entry for entry in damaged["files"] if entry["path"] != victim]
    elif damage == "duplicate":
        damaged["files"].append(damaged["files"][0])
    elif damage == "outside":
        damaged["files"][0]["path"] = "../outside.txt"
    else:
        damaged["revision"] += 1
    runctl.write_json_atomic(root / exports.MANIFEST, damaged)
    before = (root / "plan.json").read_bytes()
    with runctl.RunLease(completed), pytest.raises(runctl.RunError):
        exports.recover(root)
    assert exports.pending(root) and (root / "plan.json").read_bytes() == before
    runctl.write_json_atomic(root / exports.MANIFEST, valid)
    runs.status_of(completed)
    assert_consistent(completed, 1)


def test_log_failure_does_not_repeat_committed_edit(completed, monkeypatch):
    monkeypatch.setattr(runctl.EventLog, "event", lambda *a, **kw: (_ for _ in ()).throw(OSError("log full")))
    plan = rebuild(completed, {"op": "drop", "row": 1}, expected_revision=0)
    assert plan["revision"] == 1
    with pytest.raises(runctl.RunError, match="刷新"):
        rebuild(completed, {"op": "drop", "row": 1}, expected_revision=0)
    assert_consistent(completed, 1)


def test_killed_editor_process_recovers_payload_in_another_process(completed, tmp_runs):
    code = '''
import json, os, sys, threading
from pathlib import Path
sys.path[:] = PATHS
from cut_agent import exports, runctl
from cut_agent.editing import rebuild
runctl.set_runs_root_for_test(Path(sys.argv[1]))
copy = exports._copy_atomic
def interrupted(source, target):
    copy(source, target)
    if target.name == 'plan.md':
        print(json.dumps({'pid': os.getpid()}), flush=True)
        threading.Event().wait(30)
exports._copy_atomic = interrupted
rebuild(sys.argv[2], {'op': 'duration', 'row': 1, 'seconds': 1})
'''.replace("PATHS", repr(sys.path))
    child = subprocess.Popen([sys._base_executable, "-c", code, str(tmp_runs), completed],
                             stdout=subprocess.PIPE, text=True)
    try:
        report = json.loads(child.stdout.readline())
        assert report["pid"] == child.pid
        assert exports.pending(runctl.run_dir(completed))
        with pytest.raises(runctl.RunError):
            runctl.RunLease(completed)
    finally:
        child.terminate()
        child.wait(timeout=5)
        child.stdout.close()
    runs.status_of(completed)
    assert assert_consistent(completed, 1)["timeline"][0]["use_duration"] == 1


def test_missing_ready_preview_returns_not_found(completed):
    root = runctl.run_dir(completed)
    plan = load_plan(completed)
    plan["preview"] = {"status": "ready"}
    runctl.write_json_atomic(root / "plan.json", plan)
    with TestClient(make_app()) as client:
        assert client.get(f"/api/runs/{completed}/preview").status_code == 404
