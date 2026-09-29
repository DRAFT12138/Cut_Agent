"""The finishing ZIP must travel with every document-linked asset."""
from io import BytesIO
import re
from zipfile import ZipFile

from fastapi.testclient import TestClient
from bs4 import BeautifulSoup

from cut_agent import runctl, runs
from cut_agent.server import make_app
from conftest import COPY, SAMPLE_MEDIA


def test_handoff_download_is_a_consistent_portable_snapshot(tmp_runs, fake_llm):
    handle, _ = runs.start_run(str(SAMPLE_MEDIA), COPY,
                               options={"finishing_llm": False}, sync=True)
    run_id = handle.run_id
    root = runctl.run_dir(run_id)
    original_plan = (root / "plan.json").read_bytes()
    with TestClient(make_app()) as client:
        state = client.get(f"/api/runs/{run_id}/review").json()
        first = state["items"][0]["text"]
        checked = client.put(f"/api/runs/{run_id}/review", json={
            "expected_revision": state["plan_revision"], "index": 0,
            "text": first, "checked": True})
        assert checked.status_code == 200, checked.text

        response = client.get(f"/api/runs/{run_id}/handoff.zip")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/zip")
        assert "attachment;" in response.headers["content-disposition"]
        with ZipFile(BytesIO(response.content)) as bundle:
            names = set(bundle.namelist())
            assert {"交接说明.md", "精剪交接.html", "精剪指导.md", "精剪核对记录.md", "粗剪方案.md",
                    "plan.json", "cut_lines.txt"} <= names
            html = BeautifulSoup(bundle.read("精剪交接.html"), "html.parser")
            assert html.select_one('meta[http-equiv="Content-Security-Policy"]')
            assert {a.get("href") for a in html.select('nav a')} >= {
                "#cut-plan", "#cut-guide", "#cut-review"}
            assert html.select_one("#cut-plan-section-2")
            assert html.select_one("#cut-guide-section-1")
            assert html.select_one('#cut-guide a[href="#cut-plan"]')
            assert all(img.get("src") in names for img in html.select(".document img"))
            assert html.select(".document input[type=checkbox][disabled]")
            assert html.select_one("#cut-review input[type=checkbox][checked][disabled]")
            assert "frames/storyboard_0001.png" in names
            for document in ("精剪指导.md", "精剪核对记录.md", "粗剪方案.md"):
                for image in re.findall(r"!\[[^]]*\]\(<([^>]+)>\)", bundle.read(document).decode()):
                    assert image in names, (document, image)
            assert not any(name.endswith(".mp4") for name in names)
            assert bundle.read("plan.json") == original_plan
            assert bundle.read("cut_lines.txt") == (root / "cut_lines.txt").read_bytes()
            assert bundle.read("frames/storyboard_0001.png") == (root / "frames/storyboard_0001.png").read_bytes()
            assert f"- [x] {first}" in bundle.read("精剪核对记录.md").decode()
            assert f"- [ ] {first}" in bundle.read("精剪指导.md").decode()
            assert "原始拍摄素材" in bundle.read("交接说明.md").decode()
            assert "sample_media" not in names
        assert (root / "plan.json").read_bytes() == original_plan

        doc = root / "粗剪方案.md"
        original_doc = doc.read_text(encoding="utf-8")
        preview = root / "preview.mp4"
        preview.write_bytes(b"preview fixture")
        doc.write_text(original_doc + "\n[播放粗剪预览](preview.mp4)\n", encoding="utf-8")
        with ZipFile(BytesIO(client.get(f"/api/runs/{run_id}/handoff.zip").content)) as bundle:
            assert bundle.read("preview.mp4") == b"preview fixture"
            assert BeautifulSoup(bundle.read("精剪交接.html"), "html.parser").select_one("video[src='preview.mp4']")
        preview.unlink()

        doc.write_text(original_doc + "\n<script>alert('unexpected')</script>\n", encoding="utf-8")
        with ZipFile(BytesIO(client.get(f"/api/runs/{run_id}/handoff.zip").content)) as bundle:
            html = bundle.read("精剪交接.html").decode()
            assert "&lt;script&gt;" in html and "<script>" not in html
        doc.write_text(original_doc, encoding="utf-8")

        # A link in the published document must never pull a file outside the
        # handoff asset tree, and a missing frame must fail before downloading.
        doc.write_text(original_doc + "\n![outside](<frames/../../input.json>)\n", encoding="utf-8")
        blocked = client.get(f"/api/runs/{run_id}/handoff.zip")
        assert blocked.status_code == 409 and "越界" in blocked.json()["detail"]
        doc.write_text(original_doc, encoding="utf-8")
        missing = root / "frames/storyboard_0001.png"
        missing.unlink()
        blocked = client.get(f"/api/runs/{run_id}/handoff.zip")
        assert blocked.status_code == 409 and "缺失" in blocked.json()["detail"]
        assert client.get("/api/runs/does-not-exist/handoff.zip").status_code == 404
        assert not (runctl.runs_root() / "does-not-exist").exists()
