from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading

from PIL import Image
import pytest

from cut_agent import graph, webfetch
from cut_agent.media import _ffmpeg, MediaError


@pytest.fixture
def media_server():
    encoded = BytesIO()
    Image.new("RGB", (640, 480), "blue").save(encoded, "PNG")
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            content = {"/live": encoded.getvalue(), "/html": b"<html>Verify you are human</html>",
                       "/large": b"x" * 4096}.get(self.path)
            if content is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_live_image_download_and_cache_reuse(tmp_path, media_server):
    url, hits = media_server
    asset = {"name": "live", "url": url + "/live"}
    first = webfetch.acquire(asset, tmp_path / "A", tmp_path / "cache")
    assert first["status"] == "verified"
    assert first["width"] == 640 and first["height"] == 480
    assert Path(first["local"]).is_file()
    requests_before = len(hits)
    Path(first["local"]).unlink()
    second = webfetch.acquire(asset, tmp_path / "B", tmp_path / "cache")
    assert second["status"] == "verified" and Path(second["local"]).is_file()
    assert len(hits) == requests_before
    cached_payload = next((tmp_path / "cache").glob("*/media.png"))
    cached_payload.write_bytes(b"corrupted")
    third = webfetch.acquire(asset, tmp_path / "C", tmp_path / "cache")
    assert third["status"] == "verified" and len(hits) > requests_before


@pytest.mark.parametrize("route,cap", [("/missing", 10000), ("/html", 10000), ("/large", 64)])
def test_dead_html_and_oversize_remain_manual(tmp_path, media_server, route, cap):
    url, _ = media_server
    result = webfetch.acquire({"name": "bad", "url": url + route}, tmp_path / "run", tmp_path / "cache", max_bytes=cap)
    assert result["status"] == "manual" and result["error"]
    assert "local" not in result
    assert result["url"] == url + route


def test_video_probe_decode_and_resolution(tmp_path):
    try:
        ffmpeg = _ffmpeg()
    except MediaError:
        pytest.skip("ffmpeg required for real video validation")
    for size, valid in (("640x480", True), ("320x240", False)):
        video = tmp_path / f"{size}.mp4"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=red:s={size}:d=0.2", "-c:v", "libx264", str(video)], check=True)
        if valid:
            info = webfetch.inspect_media(video)
            assert info["kind"] == "video" and info["duration"] > 0
        else:
            with pytest.raises(ValueError, match="480"):
                webfetch.inspect_media(video)


def test_graph_binds_validated_file_and_keeps_failed_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "ows_client", lambda: type("Client", (), {"health": lambda self: True})())
    def search(row, query, assets):
        assets.append({"name": query, "url": "https://example.com/asset", "for_segment": row["segment_text"]})
    monkeypatch.setattr(graph, "_explore_via_owsearch", search)
    def acquire(asset, *args):
        if asset["for_segment"] == "dead":
            return {**asset, "status": "manual", "error": "404"}
        return {**asset, "status": "verified", "kind": "video", "duration": 2,
                "fps": 25, "local": str(tmp_path / "live.mp4")}
    monkeypatch.setattr(graph, "acquire_web", acquire)
    state = {"timeline": [{"source": "web", "needs_web": True, "segment_text": text, "use_duration": 5}
                          for text in ("live", "dead")], "media": [], "segments": []}
    result = graph.explore_web(state)
    first, second = result["timeline"]
    assert first["ref"] == str(tmp_path / "live.mp4")
    assert first["use_duration"] == 2 and not first["needs_web"]
    assert second["asset_status"] == "manual" and second["needs_web"]
    assert state["timeline"][0]["needs_web"]


def test_same_narration_does_not_reuse_another_rows_web_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "ows_client", lambda: type("Client", (), {"health": lambda self: True})())
    monkeypatch.setattr(graph, "_explore_via_owsearch", lambda row, query, assets: assets.append({
        "name": f"asset-{row['seq']}", "url": f"https://example.com/{row['seq']}.mp4",
        "for_segment": row["segment_text"]}))
    def acquire(asset, *args):
        if asset["for_seq"] == 2:
            return {**asset, "status": "manual", "error": "404"}
        return {**asset, "status": "verified", "kind": "video", "duration": 2,
                "fps": 25, "local": str(tmp_path / "first.mp4")}
    monkeypatch.setattr(graph, "acquire_web", acquire)
    state = {"timeline": [{"seq": seq, "source": "web", "needs_web": True,
                           "segment_text": "same narration", "use_duration": 5}
                          for seq in (1, 2)], "media": [], "segments": []}
    result = graph.explore_web(state)
    assert [asset["for_seq"] for asset in result["web_assets"]] == [1, 2]
    rows = {row["seq"]: row for row in result["timeline"]}
    assert rows[1]["asset_status"] == "verified" and rows[1]["ref"] == str(tmp_path / "first.mp4")
    assert rows[2]["asset_status"] == "manual" and rows[2]["needs_web"]
    assert not rows[2].get("ref")


def test_no_local_media_keeps_every_narration():
    result = graph.build_timeline({"media": [], "segments": [{"text": "one"}, {"text": "two"}]})
    assert [r["segment_text"] for r in result["timeline"]] == ["one", "two"]
    assert all(r["needs_web"] for r in result["timeline"])
