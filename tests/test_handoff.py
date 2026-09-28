"""The finishing ZIP must travel with every document-linked asset."""
from io import BytesIO
import json
import os
import re
from zipfile import ZipFile

from fastapi.testclient import TestClient
from bs4 import BeautifulSoup

from cut_agent import handoff, runctl, runs
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
        direct_sources = client.get(f"/api/runs/{run_id}/sources.md")
        assert direct_sources.status_code == 200, direct_sources.text
        assert "attachment;" in direct_sources.headers["content-disposition"]
        assert direct_sources.headers["cache-control"] == "no-store"
        source_check = client.get(f"/api/runs/{run_id}/sources")
        assert source_check.status_code == 200, source_check.text
        assert source_check.headers["cache-control"] == "no-store"
        source_state = source_check.json()
        assert source_state["revision"] == 0
        assert len(source_state["pictures"]) == len(json.loads(original_plan)["timeline"])
        assert all(item["availability"] == "available" and item["snapshot"] == "match"
                   for item in source_state["pictures"])

        response = client.get(f"/api/runs/{run_id}/handoff.zip")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/zip")
        assert "attachment;" in response.headers["content-disposition"]
        with ZipFile(BytesIO(response.content)) as bundle:
            names = set(bundle.namelist())
            assert {"交接说明.md", "精剪交接.html", "素材交接清单.md", "精剪指导.md", "精剪核对记录.md", "粗剪方案.md",
                    "plan.json", "cut_lines.txt"} <= names
            html = BeautifulSoup(bundle.read("精剪交接.html"), "html.parser")
            assert html.select_one('meta[http-equiv="Content-Security-Policy"]')
            assert {a.get("href") for a in html.select('nav a')} >= {
                "#cut-sources", "#cut-plan", "#cut-guide", "#cut-review"}
            assert html.select_one("#cut-sources table")
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
            manifest = bundle.read("素材交接清单.md").decode()
            assert manifest == direct_sources.text
            assert "sample_media" in manifest and "文件字节数" in manifest
            assert "任务输入快照" in manifest and " | 一致 | " in manifest
            assert "配乐源文件" in manifest
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
        assert client.get("/api/runs/does-not-exist/sources.md").status_code == 404
        assert client.get("/api/runs/does-not-exist/sources").status_code == 404
        assert not (runctl.runs_root() / "does-not-exist").exists()


def test_source_manifest_names_files_to_carry_and_marks_missing_assets(tmp_path):
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"local media")
    web = tmp_path / "downloaded.jpg"
    web.write_bytes(b"web media")
    music = tmp_path / "theme.mp3"
    music.write_bytes(b"music")
    plan = {"revision": 2, "media_folder": str(tmp_path), "timeline": [
        {"media": local.name, "source": "local"},
        {"media": "web_1", "source": "web", "asset_status": "verified",
         "local_path": str(web), "source_url": "https://example.test/media.jpg"},
        {"media": "web_2", "source": "web", "asset_status": "manual"},
        {"media": "missing.mp4", "source": "local"},
    ], "music": {"downloads": [
        {"title": "theme", "local": str(music)},
        {"title": "lost", "local": str(tmp_path / "lost.mp3")},
    ]}}
    original_stat = local.stat()
    snapshot = [{"name": local.name, "size": original_stat.st_size,
                 "mtime_ns": original_stat.st_mtime_ns},
                {"name": "missing.mp4", "size": 42, "mtime_ns": 1}]
    check = handoff._source_check(plan, snapshot)
    assert check["revision"] == 2
    assert [(item["availability"], item["snapshot"]) for item in check["pictures"]] == [
        ("available", "match"), ("available", "not_applicable"),
        ("manual", "not_applicable"), ("missing", "missing")]
    assert [item["availability"] for item in check["music"]] == ["available", "missing"]
    manifest = handoff._source_manifest(plan, snapshot)
    rows = manifest.splitlines()
    assert "方案修订：2" in manifest
    assert any("clip.mp4 | 本地 |" in row and "| 11 | 本地文件存在 | 一致 |" in row for row in rows)
    assert any("web_1 | 网络 |" in row and "| 9 | 已验证入库 | - |" in row for row in rows)
    assert "https://example.test/media.jpg" in manifest
    assert "web_2 | 网络 | - | - | 待人工替换" in manifest
    assert "missing.mp4" in manifest and "本地文件缺失 | 无法核对" in manifest
    assert any("theme |" in row and "| 5 | 文件存在" in row for row in rows)
    assert "lost.mp3" in manifest and "文件缺失" in manifest
    os.utime(local, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000))
    changed = handoff._source_manifest(plan, snapshot)
    assert any("clip.mp4 | 本地 |" in row and "| 本地文件存在 | 已变化，需复核 |" in row
               for row in changed.splitlines())
    assert handoff._source_check(plan, snapshot)["pictures"][0]["snapshot"] == "changed"
