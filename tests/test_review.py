"""Manual finishing checks stay durable without changing the generated plan."""
from fastapi.testclient import TestClient

from cut_agent import runctl, runs
from cut_agent.server import make_app
from conftest import COPY, SAMPLE_MEDIA


def test_review_survives_refresh_and_exports_checked_guide(tmp_runs, fake_llm):
    handle, _ = runs.start_run(str(SAMPLE_MEDIA), COPY,
                               options={"finishing_llm": False}, sync=True)
    run_id = handle.run_id
    before = (runctl.run_dir(run_id) / "plan.json").read_bytes()
    url = f"/api/runs/{run_id}/review"
    with TestClient(make_app()) as client:
        initial = client.get(url).json()
        assert initial["plan_revision"] == 0
        assert len(initial["items"]) >= 4
        assert not any(item["checked"] for item in initial["items"])
        assert initial["updated_at"] is None

        first = initial["items"][0]["text"]
        saved = client.put(url, json={"expected_revision": 0, "index": 0,
                                      "text": first, "checked": True})
        assert saved.status_code == 200, saved.text
        assert saved.json()["items"][0]["checked"] is True
        assert client.get(url).json()["items"][0]["checked"] is True
        assert (runctl.run_dir(run_id) / "plan.json").read_bytes() == before
        guide = (runctl.run_dir(run_id) / "精剪指导.md").read_text(encoding="utf-8")
        assert f"- [ ] {first}" in guide

        download = client.get(f"/api/runs/{run_id}/review.md")
        assert download.status_code == 200
        assert "attachment;" in download.headers["content-disposition"]
        assert f"- [x] {first}" in download.text
        assert "已核对 1/" in download.text
        assert "[查看粗剪方案与素材目录](粗剪方案.md)" in download.text
        assert client.put(url, json={"expected_revision": 0, "index": 0,
                                     "text": "其他项目", "checked": False}).status_code == 409
        assert client.put(url, json={"expected_revision": 0, "index": 0,
                                     "text": first, "checked": "yes"}).status_code == 422

        second = initial["items"][1]["text"]
        updated = client.put(url, json={"expected_revision": 0, "index": 1,
                                        "text": second, "checked": True})
        assert updated.status_code == 200
        assert [item["checked"] for item in updated.json()["items"][:2]] == [True, True]

        rebuilt = client.post(f"/api/runs/{run_id}/rebuild")
        assert rebuilt.status_code == 200, rebuilt.text
        unchanged_guide = client.get(url).json()
        assert unchanged_guide["plan_revision"] == 1
        assert unchanged_guide["guide_digest"] == initial["guide_digest"]
        assert [item["checked"] for item in unchanged_guide["items"][:2]] == [True, True]


def test_review_resets_after_timeline_change_and_rejects_stale_revision(tmp_runs, fake_llm):
    handle, _ = runs.start_run(str(SAMPLE_MEDIA), COPY,
                               options={"finishing_llm": False}, sync=True)
    run_id = handle.run_id
    url = f"/api/runs/{run_id}/review"
    with TestClient(make_app()) as client:
        initial = client.get(url).json()
        first = initial["items"][0]["text"]
        assert client.put(url, json={"expected_revision": 0, "index": 0,
                                     "text": first, "checked": True}).status_code == 200
        plan = client.get(f"/api/runs/{run_id}/plan").json()
        duration = plan["timeline"][0]["use_duration"] - .5
        edited = client.post(f"/api/runs/{run_id}/edit", json={"op": "duration", "row": 1,
                                                             "seconds": duration, "expected_revision": 0})
        assert edited.status_code == 200, edited.text
        assert edited.json()["revision"] == 1
        refreshed = client.get(url).json()
        assert refreshed["plan_revision"] == 1
        assert refreshed["reset_for_new_guide"] is True
        assert not any(item["checked"] for item in refreshed["items"])
        assert client.put(url, json={"expected_revision": 0, "index": 0,
                                     "text": first, "checked": True}).status_code == 409
        assert client.put(url, json={"expected_revision": 1, "index": 0,
                                     "text": refreshed["items"][0]["text"], "checked": True}).status_code == 200
        assert f"- [x] {first}" in client.get(f"/api/runs/{run_id}/review.md").text


def test_damaged_review_is_not_silently_reset(tmp_runs, fake_llm):
    handle, _ = runs.start_run(str(SAMPLE_MEDIA), COPY,
                               options={"finishing_llm": False}, sync=True)
    run_id = handle.run_id
    (runctl.run_dir(run_id) / "review.json").write_text("{broken", encoding="utf-8")
    with TestClient(make_app()) as client:
        response = client.get(f"/api/runs/{run_id}/review")
        assert response.status_code == 409
        assert "损坏" in response.json()["detail"]
        (runctl.run_dir(run_id) / "review.json").write_text('{"schema_version":1,"checked":["yes"]}', encoding="utf-8")
        assert client.get(f"/api/runs/{run_id}/review").status_code == 409
