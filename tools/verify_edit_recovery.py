"""Edit a review run, interrupt publication, then verify offline recovery.

This intentionally changes the given run: moves its first row to the end and
re-renders its opt-in preview. Use a fresh verify_delivery.py sample run.
"""
import json
import sys
from unittest.mock import patch

from fastapi.testclient import TestClient

from cut_agent import exports, graph, runctl, runs
from cut_agent.editing import load_plan, rebuild
from cut_agent.server import make_app


def main(run_id):
    root = runctl.run_dir(run_id).resolve()
    original = load_plan(run_id)
    before = original.get("revision", 0)
    original_timeline = original["timeline"]
    assert len(original_timeline) > 1
    injected = []
    copy = exports._copy_atomic
    def interrupt(source, target):
        copy(source, target)
        if target == root / "plan.md" and not injected:
            injected.append(True)
            raise OSError("verification: publication interrupted after plan.md")
    def no_llm(*args, **kwargs):
        raise AssertionError("edit/recovery must not call a model")
    with patch.object(graph, "chat_json", no_llm):
        with patch.object(exports, "_copy_atomic", interrupt):
            try:
                rebuild(run_id, {"op": "move", "row": 1, "to": len(original_timeline)},
                        expected_revision=before, preview=True)
            except OSError as exc:
                assert "verification:" in str(exc)
            else:
                raise AssertionError("fault was not injected")
        assert exports.pending(root)
        with runctl.RunLease(run_id), TestClient(make_app()) as client:
            assert client.get(f"/api/runs/{run_id}").json()["export_pending"]
            assert client.get(f"/api/runs/{run_id}/plan").status_code == 404
            assert client.get("/api/file", params={"path": str(root / "plan.md")}).status_code == 503
        runs.resume_run(run_id).wait(10)
    plan = load_plan(run_id)
    assert plan["revision"] == before + 1 and not exports.pending(root)
    assert plan["preview"]["status"] == "ready"
    assert [r["segment_text"] for r in plan["timeline"]] == [
        r["segment_text"] for r in original_timeline[1:] + original_timeline[:1]]
    assert runs.rebuild_state(run_id)["timeline"] == plan["timeline"]
    for stage in ("build_timeline", "explore_web", "write_doc"):
        assert runs.stage_artifact(run_id, stage)["timeline"] == plan["timeline"]
    assert all(step["anchor"] == card for step, card in zip(plan["finishing"]["steps"], plan["storyboard"]["cards"]))
    report = {"run_id": run_id, "result": "passed", "before_revision": before, "after_revision": plan["revision"],
              "fault": "after plan.md atomic replacement", "read_barrier": "plan404/file503",
              "recovered_by": "resume", "preview_seconds": plan["preview"]["duration"],
              "stages": runs.status_of(run_id)["stages_done"], "llm_calls": 0,
              "scope": "real media/export/recovery and HTTP; browser interactions not verified"}
    runctl.write_json_atomic(root / "edit-verification.json", report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
