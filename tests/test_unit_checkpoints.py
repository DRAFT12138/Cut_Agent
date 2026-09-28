from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import requests

from cut_agent import graph, llm, runctl, runs
from cut_agent.checkpoints import media_snapshot


def test_vision_pause_reuses_completed_video(tmp_path, monkeypatch):
    for name in ("a.mp4", "b.mp4"):
        (tmp_path / name).touch()
    control = runctl.RunControl("test")
    ctx = graph.RunCtx("test", control=control, run_dir=tmp_path / "run")
    state = {"media_folder": str(tmp_path), "media": [
        {"name": n, "kind": "video", "duration": 4} for n in ("a.mp4", "b.mp4")], "_ctx": ctx}
    calls = []

    def analyse(src, *a, **kw):
        calls.append(src.name)
        if len(calls) == 1:
            control.request_pause()
        return SimpleNamespace(shots=[], scene_cuts=[], frames=[], description=src.name)

    monkeypatch.setattr(graph, "VISION_ENABLED", True)
    monkeypatch.setattr(graph, "analyse_with_llm", analyse)
    with pytest.raises(runctl.RunHalted):
        graph.understand_media(state)
    ctx.control = runctl.RunControl("test")
    patch = graph.understand_media(state)
    assert calls == ["a.mp4", "b.mp4"]
    assert [m["description"] for m in patch["media"]] == calls
    assert len(patch["log"]) == 2


def test_web_pause_reuses_query_and_browser_arguments(tmp_path, monkeypatch):
    control = runctl.RunControl("test")
    ctx = graph.RunCtx("test", control=control, run_dir=tmp_path)
    calls = []

    class Browser:
        def start(self): return self
        def stop(self): pass
        def screenshot(self, name): pass
        def find_images(self, query, count):
            calls.append(query)
            if len(calls) == 1:
                control.request_pause()
            return [{"url": f"https://example.com/{len(calls)}.jpg"}]

    monkeypatch.setattr(graph, "controller", Browser)
    monkeypatch.setattr(graph, "_query_for", lambda t, segs: t["segment_text"])
    state = {"_ctx": ctx, "timeline": [
        {"source": "web", "segment_text": x} for x in ("one", "two")]}
    with pytest.raises(runctl.RunHalted):
        graph.explore_web(state)
    ctx.control = runctl.RunControl("test")
    patch = graph.explore_web(state)
    assert calls == ["one", "two"]
    assert [a["for_segment"] for a in patch["web_assets"]] == calls
    # All completed units must remain available when both services are offline.
    monkeypatch.setattr(graph, "ows_client", lambda: pytest.fail("must reuse all queries"))
    assert graph.explore_web(state)["web_assets"] == patch["web_assets"]


def test_retry_pause_does_not_send_second_request(monkeypatch):
    control = runctl.RunControl("test")
    calls = []
    def post(*a, **kw):
        calls.append(1)
        control.request_pause()
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(llm.requests, "post", post)
    with runctl.control_scope(control), pytest.raises(runctl.RunHalted):
        llm.chat("system", "user")
    assert calls == [1]
    # Scope cleanup: cancellation must not leak to a subsequent run.
    runctl.retry_checkpoint()


def test_media_snapshot_blocks_changed_resume(tmp_path, tmp_runs, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.mp4").write_bytes(b"one")
    (source / "ignored.txt").write_text("unrelated")
    assert len(media_snapshot(str(source))) == 1
    def halt(state):
        raise runctl.RunHalted("paused", state["_ctx"].run_id)
    monkeypatch.setattr(runs, "STAGES", [("halt", halt)])
    monkeypatch.setattr(runs, "STAGE_NAMES", ["halt"])
    h, _ = runs.start_run(str(source), "copy", sync=True)
    (source / "a.mp4").write_bytes(b"changed")
    with pytest.raises(runctl.RunError, match="素材"):
        runs.resume_run(h.run_id)
    assert runs.status_of(h.run_id)["status"] == "paused"


def test_worker_lease_is_exclusive_and_released_after_process_death(tmp_runs):
    code = '''
import os, sys, time
from pathlib import Path
sys.path[:] = PATHS
from cut_agent import runctl
runctl.set_runs_root_for_test(Path(sys.argv[1]))
lease = runctl.RunLease("locked-run")
print(os.getpid(), flush=True)
time.sleep(30)
'''
    # Windows venv python.exe is a redirector: terminating it does not prove
    # that the interpreter owning the lock has exited. Launch that interpreter
    # directly, retaining the current environment's import paths.
    code = code.replace("PATHS", repr(sys.path))
    child = subprocess.Popen([sys._base_executable, "-c", code, str(tmp_runs)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert int(child.stdout.readline()) == child.pid
        with pytest.raises(runctl.RunError):
            runctl.RunLease("locked-run")
    finally:
        child.terminate()
        child.wait(timeout=5)
        child.stdout.close()
    with runctl.RunLease("locked-run"):
        with pytest.raises(runctl.RunError):
            runctl.RunLease("locked-run")


def test_degraded_stages_are_explicit(tmp_runs, fake_llm):
    fake_llm.fail_plan = True
    h, _ = runs.start_run("unused", "test", sync=True)
    meta = runs.status_of(h.run_id)
    assert meta["status"] == "done"
    by_name = {s["name"]: s for s in meta["stages"]}
    for name in ("plan_segments", "pick_music"):
        assert by_name[name]["status"] == "degraded"
        assert by_name[name]["warnings"]
        assert runs.stage_artifact(h.run_id, name)["_stage"]["warnings"]
    assert "_stage" not in runs.rebuild_state(h.run_id)
    (runctl.run_dir(h.run_id) / "run.json").write_text("{", encoding="utf-8")
    assert h.run_id in runs.recover_interrupted()
    recovered = {s["name"]: s for s in runs.status_of(h.run_id)["stages"]}
    assert recovered["plan_segments"]["status"] == "degraded"
    assert recovered["plan_segments"]["warnings"]
