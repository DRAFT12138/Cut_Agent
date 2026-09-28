"""Web 控制台后端（Phase 0，FastAPI）。

路由全部是 runs.py / runctl 的薄封装，无业务逻辑；
前端（web/，React + AntD5）构建产物若存在则挂在 /（SPA），否则给占位页 + /docs。

运行：python -m cut_agent.cli serve --port 8090
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, exports, runs, runctl
from .config import WORK_DIR
from .runctl import RunError
from pydantic import BaseModel, Field, StrictBool, StrictInt
from urllib.parse import quote


class CreateRun(BaseModel):
    media_dir: str
    copy_text: str = Field(alias="copy")
    seed: int | None = None
    preview: bool = False
    platform: Literal["douyin", "xiaohongshu", "bilibili"] = "douyin"
    finishing_llm: bool = True


class ReviewCheck(BaseModel):
    expected_revision: StrictInt
    index: StrictInt
    text: str
    checked: StrictBool


class HtmlMotionRequest(BaseModel):
    expected_revision: StrictInt
    after: StrictInt
    prompt: str = ""
    duration: float = Field(default=1.5, gt=0, le=30)
    resolution: Literal["1080p", "4k"] = "4k"
    music_mode: Literal["auto", "continuous", "transition", "none"] = "auto"
    bpm: float | None = Field(default=None, ge=30, le=300)
    preview: bool = False

WEB_DIST = Path(__file__).resolve().parent / "web_dist"


def make_app() -> FastAPI:
    app = FastAPI(title="Cut Agent", version=__version__)

    # ---------- 基础 ----------

    @app.get("/api/stages")
    def stages() -> dict:
        """前端流程节点定义（顺序 = 执行顺序）。"""
        return {"stages": runs.STAGE_NAMES,
                "terminal": ["done", "failed", "canceled", "paused", "interrupted"]}

    @app.get("/api/runs")
    def list_runs() -> list[dict]:
        return runs.list_runs()

    @app.post("/api/runs")
    def create_run(body: CreateRun) -> dict:
        media_dir = body.media_dir.strip()
        copy = body.copy_text
        if not media_dir or not Path(media_dir).is_dir():
            raise HTTPException(400, f"media_dir 不存在: {media_dir}")
        if not copy.strip():
            raise HTTPException(400, "copy 不能为空")
        seed = body.seed
        handle, _ = runs.start_run(media_dir, copy, seed=seed,
                                  options={"preview": body.preview, "platform": body.platform,
                                           "finishing_llm": body.finishing_llm}, sync=False)
        return {"run_id": handle.run_id, "status": "running"}

    @app.get("/api/runs/{run_id}")
    def run_status(run_id: str) -> dict:
        try:
            meta = runs.status_of(run_id)
        except RunError as e:
            raise HTTPException(404, str(e))
        arts = {}
        versions = {}
        for name in runs.STAGE_NAMES:
            if runs.stage_artifact(run_id, name) is not None:
                arts[name] = True
                try:
                    stat = runctl.stage_path(run_id, name).stat()
                    versions[name] = f"{stat.st_mtime_ns}:{stat.st_size}"
                except FileNotFoundError:
                    arts.pop(name, None)
        inp = _input_of(run_id)
        meta["has_artifacts"] = arts
        meta["artifact_versions"] = versions
        meta["media_dir"] = inp.get("media_dir", "")
        meta["copy"] = inp.get("copy", "")
        meta["seed"] = inp.get("seed")
        return meta

    @app.get("/api/runs/{run_id}/stages/{name}")
    def stage_artifact(run_id: str, name: str):
        try:
            a = runs.stage_artifact(run_id, name)
        except RunError as e:
            raise HTTPException(404, str(e))
        if a is None:
            raise HTTPException(404, f"阶段 {name} 尚无产物")
        return JSONResponse(a)

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, since: int = 0) -> dict:
        try:
            runs.status_of(run_id)
        except RunError as e:
            raise HTTPException(404, str(e))
        log = runctl.EventLog(run_id)
        batch = log.tail(since)
        return {"events": batch, "latest": batch[-1]["seq"] if batch else since}

    # ---------- 任务控制 ----------

    @app.get("/api/runs/{run_id}/preview")
    def preview_file(run_id: str):
        from .editing import load_plan
        try:
            plan = load_plan(run_id)
            if (plan.get("preview") or {}).get("status") != "ready":
                raise RunError("预览不存在或已过期")
            path = runctl.run_dir(run_id) / "preview.mp4"
            if not path.is_file():
                raise RunError("预览文件缺失，请重新生成")
            return FileResponse(path, media_type="video/mp4")
        except RunError as exc:
            raise HTTPException(404, str(exc))

    @app.post("/api/runs/{run_id}/preview")
    def refresh_preview(run_id: str):
        from .editing import rebuild
        try:
            return rebuild(run_id, preview=True)
        except RunError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/runs/{run_id}/variant")
    def variant(run_id: str, body: dict):
        seed = body.get("seed")
        if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
            raise HTTPException(400, "seed 必须为整数")
        try:
            handle, _ = runs.start_variant(run_id, seed)
            return {"run_id": handle.run_id, "status": "running"}
        except RunError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/runs/{run_id}/plan")
    def plan(run_id: str):
        from .editing import load_plan
        try:
            return load_plan(run_id)
        except RunError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/api/runs/{run_id}/review")
    def get_review(run_id: str):
        from .review import get
        try:
            return get(run_id)
        except RunError as exc:
            raise HTTPException(409, str(exc))

    @app.put("/api/runs/{run_id}/review")
    def set_review(run_id: str, body: ReviewCheck):
        from .review import set_check
        try:
            return set_check(run_id, body.expected_revision, body.index, body.text, body.checked)
        except RunError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/runs/{run_id}/review.md")
    def download_review(run_id: str):
        from .review import export_markdown
        try:
            content = export_markdown(run_id)
        except RunError as exc:
            raise HTTPException(409, str(exc))
        filename = quote("精剪核对记录.md")
        return PlainTextResponse(content, media_type="text/markdown",
                                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})

    @app.get("/api/runs/{run_id}/sources.md")
    def download_sources(run_id: str):
        from .handoff import source_manifest
        if not runctl.run_dir(run_id).is_dir():
            raise HTTPException(404, f"run 不存在: {run_id}")
        try:
            content = source_manifest(run_id)
        except RunError as exc:
            raise HTTPException(409, str(exc))
        filename = quote("素材交接清单.md")
        return PlainTextResponse(content, media_type="text/markdown",
                                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                                          "Cache-Control": "no-store"})

    @app.get("/api/runs/{run_id}/sources")
    def check_sources(run_id: str):
        from .handoff import source_check
        if not runctl.run_dir(run_id).is_dir():
            raise HTTPException(404, f"run 不存在: {run_id}")
        try:
            content = source_check(run_id)
        except RunError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse(content, headers={"Cache-Control": "no-store"})

    @app.get("/api/runs/{run_id}/handoff.zip")
    def download_handoff(run_id: str):
        from .handoff import build
        try:
            if not runctl.run_dir(run_id).is_dir():
                raise HTTPException(404, f"run 不存在: {run_id}")
            archive = build(run_id)
        except RunError as exc:
            raise HTTPException(409, str(exc))
        archive.seek(0, 2)
        size = archive.tell()
        archive.seek(0)

        def chunks():
            try:
                while part := archive.read(1024 * 1024):
                    yield part
            finally:
                archive.close()

        filename = quote("精剪交接包.zip")
        return StreamingResponse(chunks(), media_type="application/zip",
                                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                                          "Content-Length": str(size), "Cache-Control": "no-store"})

    @app.post("/api/runs/{run_id}/edit")
    def edit(run_id: str, body: dict):
        from .editing import rebuild
        try:
            revision = body.pop("expected_revision", None)
            return rebuild(run_id, body, expected_revision=revision)
        except RunError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/runs/{run_id}/html-motion")
    def html_motion(run_id: str, body: HtmlMotionRequest):
        from .editing import add_html_motion
        try:
            return add_html_motion(run_id, after=body.after, prompt=body.prompt,
                                   duration=body.duration, resolution=body.resolution,
                                   expected_revision=body.expected_revision,
                                   preview=body.preview, music_mode=body.music_mode, bpm=body.bpm)
        except RunError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/runs/{run_id}/rebuild")
    def rebuild_plan(run_id: str):
        from .editing import rebuild
        try:
            return rebuild(run_id)
        except RunError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/runs/{run_id}/pause")
    def pause(run_id: str) -> dict:
        try:
            runs.pause_run(run_id)
        except RunError as e:
            raise HTTPException(409, str(e))
        return {"run_id": run_id, "status": "pausing"}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict:
        try:
            runs.cancel_run(run_id)
        except RunError as e:
            raise HTTPException(409, str(e))
        return {"run_id": run_id, "status": "canceling"}

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str) -> dict:
        try:
            h = runs.resume_run(run_id)
        except RunError as e:
            raise HTTPException(409, str(e))
        return {"run_id": run_id, "status": "running"}

    # ---------- 文件（帧/文档/预览 mp3 等） ----------
    # 允许访问：runs 根、work 根（下载物）。其余 403。

    @app.get("/api/file")
    def get_file(path: str, run: str | None = None) -> FileResponse:
        """静态文件访问：runs 根 / work 根 / 指定 run 的素材目录（run=run_id）。"""
        p = Path(path).expanduser().resolve()
        roots = [runctl.runs_root(), WORK_DIR]
        if run:
            md = _input_of(run).get("media_dir")
            if md:
                roots.append(Path(md).resolve())
        if not any(p.is_relative_to(r) for r in roots if r is not None):
            raise HTTPException(403, "路径不在允许范围")
        runs_root = runctl.runs_root().resolve()
        if p.is_relative_to(runs_root):
            relative = p.relative_to(runs_root)
            if relative.parts:
                owner = relative.parts[0]
                if exports.pending(runs_root / owner):
                    runs.recover_interrupted(owner)
                    if exports.pending(runs_root / owner):
                        raise HTTPException(503, "编辑导出正在恢复，请稍后重试")
        if not p.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(p)

    mount_dist(app)
    return app


def _input_of(run_id: str) -> dict:
    d = runctl.read_json(runctl.run_dir(run_id) / "input.json")
    return d or {}


def mount_dist(app: FastAPI) -> None:
    """Mount the frontend embedded in the installed Python package."""
    if WEB_DIST.is_dir():
        assets = WEB_DIST / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(404, "未知 API")
            f = (WEB_DIST / full_path).resolve()
            if not f.is_relative_to(WEB_DIST.resolve()):
                raise HTTPException(403, "路径不在允许范围")
            if full_path and f.is_file():
                return FileResponse(f)
            return FileResponse(WEB_DIST / "index.html")
    else:

        @app.get("/")
        def placeholder():
            return PlainTextResponse(
                "Cut Agent API 运行中。安装包缺少前端资源，请重新安装正式发行物。\n"
                "API 文档: /docs\n")
