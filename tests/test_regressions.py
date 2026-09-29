"""Public boundary regressions: no LLM, network, or local media required."""
import argparse
import threading

import pytest
from fastapi.testclient import TestClient

from cut_agent import cli, graph, llm, runctl, runs
from cut_agent.server import make_app


def test_seed_reaches_http_payload(monkeypatch):
    payloads = []

    class Response:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    def post(url, **kw):
        payloads.append(kw["json"])
        return Response()

    monkeypatch.setattr(llm.requests, "post", post)
    assert llm.chat_json("system", "user", seed=0) == {"ok": True}
    llm.chat_json("system", "user")
    assert payloads[0]["seed"] == 0
    assert "seed" not in payloads[1]


def test_status_single_id(tmp_runs, monkeypatch):
    seen = []
    monkeypatch.setattr(runs, "status_of", lambda rid: seen.append(rid) or
                        {"id": rid, "status": "done", "stages": []})
    assert cli.cmd_status(argparse.Namespace(run_id="one-run")) == 0
    assert seen == ["one-run"]


def test_event_pagination_and_missing_run(tmp_runs):
    rd = runctl.run_dir("events-run")
    runctl.write_json_atomic(rd / "run.json", {"id": "events-run", "stages": []})
    log = runctl.EventLog("events-run")
    for i in range(520):
        log.event("scan_media", "log", msg=str(i))
    with TestClient(make_app()) as client:
        first = client.get("/api/runs/events-run/events?since=0").json()
        second = client.get(f"/api/runs/events-run/events?since={first['latest']}").json()
        assert [e["seq"] for e in first["events"] + second["events"]] == list(range(1, 521))
        assert client.get("/api/runs/missing/events").status_code == 404
        assert not (tmp_runs / "missing").exists()


@pytest.mark.parametrize("rid", ["..", "../outside", "C:\\outside", "a/b", ""])
def test_invalid_run_id(tmp_runs, rid):
    with pytest.raises(runctl.RunError):
        runctl.run_dir(rid)


def test_corrupt_metadata_and_checkpoint_gap(tmp_runs):
    rd = runctl.run_dir("broken-run")
    runctl.write_json_atomic(rd / "input.json", {"media_dir": "unused", "copy": "test"})
    (rd / "run.json").write_text('{"broken":', encoding="utf-8")
    runctl.write_json_atomic(rd / "stages/scan_media.json", {"media": [{"name": "good"}]})
    runctl.write_json_atomic(rd / "stages/understand_media.json", {"media": [{"name": "stale"}]})
    assert runs.recover_interrupted() == ["broken-run"]
    assert runs.status_of("broken-run")["resumable_from"] == "plan_segments"
    assert runs.rebuild_state("broken-run")["media"] == [{"name": "good"}]


def test_cancel_at_stage_boundary_preserves_checkpoint(tmp_runs, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def first(state):
        entered.set()
        assert release.wait(5)
        return {"segments": [], "log": ["saved first stage"]}

    def second(state):
        calls.append("second")
        return {}

    monkeypatch.setattr(runs, "STAGES", [("first", first), ("second", second)])
    monkeypatch.setattr(runs, "STAGE_NAMES", ["first", "second"])
    h, _ = runs.start_run("unused", "test")
    assert entered.wait(5)
    runs.cancel_run(h.run_id)
    release.set()
    h.wait(5)
    assert runs.status_of(h.run_id)["status"] == "canceled"
    assert runs.first_incomplete(h.run_id) == 1
    assert not calls
    assert any(e.get("msg") == "saved first stage" for e in h.log.tail())
    resumed = runs.resume_run(h.run_id)
    resumed.wait(5)
    assert calls == ["second"]
    assert runs.status_of(h.run_id)["status"] == "done"


def test_music_does_not_swallow_pause(tmp_runs, monkeypatch):
    control = runctl.RunControl("music-run")
    def recommend(*a, **kw):
        control.request_pause()
        return {}
    monkeypatch.setattr(graph, "chat_json", recommend)
    with pytest.raises(runctl.RunHalted):
        graph.pick_music({"_ctx": graph.RunCtx(run_id="music-run", control=control)})


def test_invalid_api_inputs_are_client_errors(tmp_runs):
    with TestClient(make_app()) as client:
        for body in ({"media_dir": 1, "copy": []},
                     {"media_dir": ".", "copy": "test", "seed": "bad"}):
            assert client.post("/api/runs", json=body).status_code == 422
