import json

import pytest
from fastapi.testclient import TestClient

from cut_agent import cli, graph, runctl, runs
from cut_agent.editing import rebuild, load_plan, patch_plan
from cut_agent.server import make_app


@pytest.fixture
def completed(tmp_runs, fake_llm, monkeypatch):
    handle, _ = runs.start_run("unused", "one two three", sync=True)
    assert runs.status_of(handle.run_id)["status"] == "done"
    monkeypatch.setattr(graph, "chat_json", lambda *a, **kw: pytest.fail("editing must not call LLM"))
    return handle.run_id


def test_direct_json_edit_rebuild_is_authoritative(completed):
    path = runctl.run_dir(completed) / "plan.json"
    plan = load_plan(completed)
    plan["timeline"][0]["segment_text"] = "Changed narration"
    path.write_text(json.dumps(plan), encoding="utf-8")
    rebuilt = rebuild(completed)
    assert rebuilt["revision"] == 1
    assert "Changed narration" in (path.parent / "plan.md").read_text(encoding="utf-8")
    assert rebuilt["critique"]["repair_passes"] == 0
    assert runs.stage_artifact(completed, "build_timeline")["timeline"] == rebuilt["timeline"]
    assert runs.stage_artifact(completed, "write_doc")["doc_path"] == str(path.parent.resolve() / "粗剪方案.md")
    assert list((path.parent / "history").glob("*.json"))


def test_edit_operations_and_invalid_update_preserve_files(completed):
    original = load_plan(completed)
    first = original["timeline"][0]["segment_text"]
    changed = rebuild(completed, {"op": "move", "row": 1, "to": 3})
    assert changed["timeline"][2]["segment_text"] == first
    changed = rebuild(completed, {"op": "duration", "row": 1, "seconds": 1})
    assert changed["timeline"][0]["use_duration"] == 1
    root = runctl.run_dir(completed)
    before = {p.name: p.read_bytes() for p in (root / "plan.json", root / "plan.md")}
    for seconds in (9999, float("nan"), -1):
        with pytest.raises(runctl.RunError):
            rebuild(completed, {"op": "duration", "row": 1, "seconds": seconds})
    assert before == {p.name: p.read_bytes() for p in (root / "plan.json", root / "plan.md")}
    name = original["media"][0]["name"]
    changed = rebuild(completed, {"op": "swap", "row": 1, "media": name})
    assert changed["timeline"][0]["media"] == name
    changed = rebuild(completed, {"op": "append", "media": name})
    assert len(changed["timeline"]) == len(original["timeline"]) + 1
    changed = rebuild(completed, {"op": "drop", "row": 1})
    assert len(changed["timeline"]) == len(original["timeline"])


def test_moving_and_dropping_rows_rebinds_web_asset_cards(completed):
    plan = load_plan(completed)
    plan["web_assets"] = [{"for_seq": 1, "status": "manual", "url": "https://example.com/one"},
                          {"for_seq": 2, "status": "verified", "url": "https://example.com/two"}]
    moved = patch_plan(plan, {"op": "move", "row": 1, "to": 3})
    assert [asset["for_seq"] for asset in moved["web_assets"]] == [3, 1]
    assert moved["timeline"][2]["segment_text"] == plan["timeline"][0]["segment_text"]
    dropped = patch_plan(moved, {"op": "drop", "row": 3})
    assert [asset["for_seq"] for asset in dropped["web_assets"]] == [0, 1]


def test_api_revision_conflict_and_cli(completed):
    with TestClient(make_app()) as client:
        assert client.get(f"/api/runs/{completed}/plan").status_code == 200
        route = f"/api/runs/{completed}/edit"
        operation = {"op": "move", "row": 1, "to": 2, "expected_revision": 0}
        assert client.post(route, json=operation).status_code == 200
        assert client.post(route, json=operation).status_code == 409
    assert cli.main(["edit", "--run", completed, "--duration", "1", "2"]) == 0
    assert cli.main(["rebuild", "--run", completed]) == 0
    assert cli.main(["edit", "--run", completed, "--drop", "999"]) == 2


def test_render_failure_leaves_authoritative_plan_intact(completed, monkeypatch):
    before = load_plan(completed)
    def fail(*a, **kw):
        raise OSError("simulated render failure")
    monkeypatch.setattr(graph, "write_doc", fail)
    with pytest.raises(OSError):
        rebuild(completed, {"op": "drop", "row": 1})
    assert load_plan(completed) == before
    with runctl.RunLease(completed):
        pass
