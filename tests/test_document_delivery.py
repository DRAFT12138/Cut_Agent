from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
from PIL import Image

from cut_agent import graph, media, runctl
from cut_agent.server import make_app


def test_scan_preserves_nested_sources_and_owns_distinct_video_thumbnails(tmp_path, monkeypatch):
    folder, root = tmp_path / "media", tmp_path / "run"
    for directory, color in (("a", "red"), ("b", "blue")):
        source = folder / directory / "same.mp4"
        source.parent.mkdir(parents=True)
        subprocess.run([media._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color={color}:s=640x480:r=25:d=0.4", "-c:v", "libx264", str(source)], check=True)
    # Exercise direct probing (shared _thumbs folder) and then snapshot those
    # exact outputs through the graph without extracting additional frames.
    probed = media.probe_media(folder)
    monkeypatch.setattr(graph, "probe_media", lambda *a, **kw: probed)
    result = graph.scan_media({"media_folder": str(folder), "_ctx": graph.RunCtx("scan", run_dir=root)})
    rows = result["media"]
    assert [row["name"] for row in rows] == ["a/same.mp4", "b/same.mp4"]
    assert all(len(row["thumbnails"]) == 3 for row in rows)
    assert len(list((folder / "_thumbs").glob("*.jpg"))) == 6
    for row in rows:
        assert all(Path(path).is_relative_to(root) for path in row["thumbnails"])
    red, blue = [Image.open(row["thumbnails"][0]).getpixel((10, 10)) for row in rows]
    assert red[0] > 200 and red[2] < 20
    assert blue[2] > 200 and blue[0] < 20
    before = Path(rows[0]["thumbnails"][0]).read_bytes()
    for path in (folder / "_thumbs").glob("*.jpg"):
        Image.new("RGB", (50, 50), "green").save(path)
    assert Path(rows[0]["thumbnails"][0]).read_bytes() == before


def test_multiline_narration_and_pipes_do_not_break_export_table(tmp_path):
    narration = "第一句\n第二句 | 仍是同一行"
    state = {"media_folder": str(tmp_path), "copy": narration, "timeline": [
        {"seq": 1, "media": "picture.png", "kind": "image", "use_duration": 4,
         "segment_text": narration, "note": "备注\r\n带 | 分隔符"}],
        "_ctx": graph.RunCtx("document", run_dir=tmp_path / "run")}
    result = graph.write_doc(state)
    text = Path(result["doc_path"]).read_text(encoding="utf-8")
    table = text.split("## 时间线明细", 1)[1].split("## 网络素材", 1)[0]
    lines = [line for line in table.splitlines() if line.startswith("|")]
    assert len(lines) == 3  # header, separator, one complete row
    assert "第一句 第二句 \\| 仍是同一行" in lines[2]
    assert "备注 带 \\| 分隔符" in lines[2]
    plan = runctl.read_json(tmp_path / "run/plan.json")
    assert plan["timeline"][0]["segment_text"] == narration


def test_document_assets_are_served_under_run_root_but_cannot_escape_it(tmp_runs, tmp_path):
    root = runctl.run_dir("document-assets")
    runctl.write_json_atomic(root / "input.json", {"media_dir": str(tmp_path / "media"), "copy": "copy"})
    (root / "frames").mkdir()
    Image.new("RGB", (10, 10), "red").save(root / "frames/shot 1.png")
    (root / "plan.md").write_text("# 方案\n\n![分镜](<frames/shot 1.png>)", encoding="utf-8")
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")
    with TestClient(make_app()) as client:
        response = client.get("/api/file", params={"path": str(root / "plan.md"), "run": "document-assets"})
        assert response.status_code == 200 and "# 方案" in response.text
        frame = client.get("/api/file", params={"path": str(root / "frames/../frames/shot 1.png"), "run": "document-assets"})
        assert frame.status_code == 200 and frame.headers["content-type"] == "image/png"
        denied = client.get("/api/file", params={"path": str(root / "../../outside.md"), "run": "document-assets"})
        assert denied.status_code == 403
