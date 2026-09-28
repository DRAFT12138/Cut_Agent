"""Build a consistent, portable finishing handoff from one published run."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
from tempfile import SpooledTemporaryFile
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from . import exports, runs, runctl
from .editing import load_plan
from .handoff_html import build_html
from .review import export_markdown


_LINK = re.compile(r"!?\[[^\]\n]*\]\((?:<([^>\n]+)>|([^\s)\n]+))\)")
_DOCS = ("精剪指导.md", "精剪核对记录.md", "粗剪方案.md", "素材交接清单.md", "精剪交接.html")
_FILES = ("plan.json", "cut_lines.txt")


def _markdown_cell(value: object) -> str:
    return " ".join(str(value if value is not None else "-").splitlines()).replace("\\", "\\\\").replace("|", "\\|")


def _file_info(path: Path | None) -> tuple[str, int | None]:
    try:
        if path and path.is_file():
            stat = path.stat()
            return str(stat.st_size), stat.st_mtime_ns
    except OSError:
        pass
    return "-", None


def _picture_sources(plan: dict, media_snapshot: list[dict] | None = None) -> list[dict]:
    """Check each current timeline row against the media present on this machine."""
    folder = Path(str(plan["media_folder"])).resolve() if plan.get("media_folder") else None
    snapshots = {str(item["name"]): item for item in media_snapshot or []
                 if isinstance(item, dict) and item.get("name")}
    sources = []
    for seq, row in enumerate(plan.get("timeline") or [], 1):
        name = str(row.get("media") or "待补素材")
        source = row.get("source") or "local"
        path: Path | None = None
        if source == "web":
            verified = row.get("asset_status") == "verified"
            if verified and row.get("local_path"):
                path = Path(str(row["local_path"])).resolve()
            size, _ = _file_info(path)
            availability = ("available" if size != "-" else "missing") if verified else "manual"
            snapshot_check = "not_applicable"
        else:
            if folder:
                candidate = (folder / name).resolve()
                if candidate.is_relative_to(folder):
                    path = candidate
            size, modified = _file_info(path)
            availability = "available" if size != "-" else "missing"
            original = snapshots.get(name)
            if size == "-":
                snapshot_check = "missing"
            elif original is None:
                snapshot_check = "unrecorded"
            elif original.get("size") == int(size) and original.get("mtime_ns") == modified:
                snapshot_check = "match"
            else:
                snapshot_check = "changed"
        sources.append({"seq": seq, "name": name, "source": source,
                        "path": str(path) if path else None,
                        "size_bytes": int(size) if size != "-" else None,
                        "availability": availability, "snapshot": snapshot_check,
                        "source_url": row.get("source_url") or None})
    return sources


def _music_sources(plan: dict) -> list[dict]:
    sources = []
    for index, item in enumerate((plan.get("music") or {}).get("downloads") or []):
        path = Path(str(item["local"])).resolve() if item.get("local") else None
        size, _ = _file_info(path)
        sources.append({"role": "preview" if index == 0 else "alternative",
                        "title": item.get("title") or "未命名配乐",
                        "path": str(path) if path else None,
                        "size_bytes": int(size) if size != "-" else None,
                        "availability": "available" if size != "-" else "missing"})
    return sources


def _source_check(plan: dict, media_snapshot: list[dict] | None = None) -> dict:
    """Structured counterpart of the downloadable checklist, for the Web handoff node."""
    return {"revision": plan.get("revision", 0),
            "pictures": _picture_sources(plan, media_snapshot),
            "music": _music_sources(plan)}


def _source_manifest(plan: dict, media_snapshot: list[dict] | None = None) -> str:
    """List the source files an editor must carry separately for this revision."""
    check = _source_check(plan, media_snapshot)
    local_labels = {"available": "本地文件存在", "missing": "本地文件缺失"}
    web_labels = {"available": "已验证入库", "missing": "已验证但文件缺失",
                  "manual": "待人工替换"}
    snapshot_labels = {"match": "一致", "changed": "已变化，需复核",
                       "missing": "无法核对", "unrecorded": "未记录",
                       "not_applicable": "-"}
    lines = ["# 素材交接清单", "",
             f"方案修订：{plan.get('revision', 0)}。本清单按当前时间线生成；媒体文件不在交接 ZIP 内。",
             "将下列原始文件另行复制，按文件名及字节数核对后在达芬奇等软件中重新定位。行号与 cut_lines.txt 对应。",
             "任务输入快照核对本地文件的原始大小与修改时间；标记“已变化，需复核”的文件不要直接当作原版。网络和配乐文件不在任务输入快照中。", "",
             "## 画面素材", "",
             "| 行号 | 素材名 | 来源 | 原机器文件路径 | 文件字节数 | 当前状态 | 任务输入快照 | 来源链接 |",
             "|---:|---|---|---|---:|---|---|---|"]
    for row in check["pictures"]:
        labels = web_labels if row["source"] == "web" else local_labels
        status = labels[row["availability"]]
        values = (row["seq"], row["name"], "网络" if row["source"] == "web" else "本地",
                  row["path"] or "-", row["size_bytes"] if row["size_bytes"] is not None else "-",
                  status, snapshot_labels[row["snapshot"]], row["source_url"] or "-")
        lines.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    lines += ["", "## 配乐源文件", ""]
    if check["music"]:
        lines += ["| 用途 | 曲目 | 原机器文件路径 | 文件字节数 | 当前状态 |",
                  "|---|---|---|---:|---|"]
        for item in check["music"]:
            values = ("预览使用" if item["role"] == "preview" else "备选", item["title"],
                      item["path"] or "-", item["size_bytes"] if item["size_bytes"] is not None else "-",
                      "文件存在" if item["availability"] == "available" else "文件缺失")
            lines.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    else:
        lines.append("当前方案没有已下载的配乐文件；如需配乐，请按精剪指导另行准备。")
    return "\n".join(lines) + "\n"


def _local_links(documents: dict[str, str]) -> set[str]:
    """Resolve generated Markdown links without letting a document package arbitrary files."""
    assets: set[str] = set()
    for name, body in documents.items():
        for match in _LINK.finditer(body):
            target = match.group(1) or match.group(2)
            if target.startswith("#"):
                continue
            parsed = urlsplit(target)
            if parsed.scheme in ("http", "https"):
                continue
            if parsed.scheme or parsed.netloc or not parsed.path:
                raise runctl.RunError(f"{name} 含不支持的本地链接：{target}")
            path = unquote(parsed.path)
            relative = PurePosixPath(path)
            if ("\\" in path or relative.is_absolute() or ".." in relative.parts
                    or not relative.parts):
                raise runctl.RunError(f"{name} 含越界链接：{target}")
            member = relative.as_posix()
            if member in (*_DOCS, *_FILES, "交接说明.md"):
                continue
            if member == "preview.mp4" or relative.parts[0] in ("frames", "web"):
                assets.add(member)
                continue
            raise runctl.RunError(f"{name} 含无法打包的本地链接：{target}")
    return assets


def _file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise runctl.RunError(f"交接文件缺失或越界：{relative}")
    return path


def _published_plan(run_id: str, root: Path) -> dict:
    current = runctl.read_json(root / "run.json")
    if (not isinstance(current, dict) or current.get("status") != "done"
            or exports.pending(root)):
        raise runctl.RunError("任务状态已变化，请稍后重新下载交接材料")
    return load_plan(run_id)


def _input_snapshot(root: Path) -> list[dict] | None:
    record = runctl.read_json(root / "input.json")
    snapshot = record.get("media_snapshot") if isinstance(record, dict) else None
    return snapshot if isinstance(snapshot, list) else None


def source_manifest(run_id: str) -> str:
    """Return the same current-revision source checklist used by the ZIP."""
    status = runs.status_of(run_id)
    if status["status"] != "done" or status.get("export_pending"):
        raise runctl.RunError("任务尚未完成或编辑导出正在恢复，暂不能生成素材清单")
    root = runctl.run_dir(run_id)
    with runctl.RunLease(run_id):
        return _source_manifest(_published_plan(run_id, root), _input_snapshot(root))


def source_check(run_id: str) -> dict:
    """Return live source availability for the same published revision as the checklist."""
    status = runs.status_of(run_id)
    if status["status"] != "done" or status.get("export_pending"):
        raise runctl.RunError("任务尚未完成或编辑导出正在恢复，暂不能核对交接素材")
    root = runctl.run_dir(run_id)
    with runctl.RunLease(run_id):
        return _source_check(_published_plan(run_id, root), _input_snapshot(root))


def build(run_id: str) -> SpooledTemporaryFile:
    """Return a seeked ZIP; the caller owns and must close the file."""
    status = runs.status_of(run_id)
    if status["status"] != "done" or status.get("export_pending"):
        raise runctl.RunError("任务尚未完成或编辑导出正在恢复，暂不能生成交接包")
    root = runctl.run_dir(run_id)
    archive = SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    try:
        with runctl.RunLease(run_id):
            plan = _published_plan(run_id, root)
            documents = {
                "素材交接清单.md": _source_manifest(plan, _input_snapshot(root)),
                "精剪指导.md": _file(root, "精剪指导.md").read_text(encoding="utf-8"),
                "精剪核对记录.md": export_markdown(run_id),
                "粗剪方案.md": _file(root, "粗剪方案.md").read_text(encoding="utf-8"),
            }
            assets = _local_links(documents)
            paths = {name: _file(root, name) for name in (*_FILES, *sorted(assets))}
            for row in plan.get("timeline", []):
                if row.get("source") != "generated" or row.get("kind") != "html":
                    continue
                generated = (root / "generated").resolve()
                pages = row.get("html_pages") or [{"path": row.get("local_path")}]
                for page in pages:
                    source = Path(str(page.get("path", ""))).resolve()
                    if not source.is_relative_to(generated) or not source.is_file() or source.suffix.lower() != ".html":
                        raise runctl.RunError("HTML 动画源文件缺失或越界")
                    paths[f"generated/{source.name}"] = source
            source_dir = plan.get("media_folder") or "未记录"
            readme = ("# 精剪交接说明\n\n"
                      f"任务：{run_id}；方案修订：{plan.get('revision', 0)}。\n\n"
                      "先按《素材交接清单.md》核对需另带的画面与配乐源文件，"
                      "阅读《粗剪方案.md》中的时间线与源入出点，再按《精剪指导.md》或"
                      "《精剪核对记录.md》在达芬奇等剪辑软件中执行精剪。"
                      "也可直接在浏览器打开《精剪交接.html》离线查看清单、文档与配图。"
                      "《plan.json》和《cut_lines.txt》可用于核对时间线或供后续工具读取；"
                      "执行清单每行对应一段画面，并列出源入出点、成片入出点及素材状态。\n\n"
                      "配图与文档保持原有相对路径；若方案中链接了粗剪预览，预览也已打包。"
                      "HTML 动画源文件位于 generated/，可再次用浏览器逐帧渲染。"
                      "原始拍摄素材、外部网络素材和配乐源文件不在包内，需要单独携带并在剪辑软件中重新定位。\n\n"
                      f"原始素材目录（仅作定位参考）：`{source_dir}`\n")
            with ZipFile(archive, "w", allowZip64=True) as bundle:
                bundle.writestr("交接说明.md", readme, compress_type=ZIP_DEFLATED)
                bundle.writestr("精剪交接.html", build_html(documents, run_id,
                                plan.get("revision", 0), "preview.mp4" in assets),
                                compress_type=ZIP_DEFLATED)
                for name, body in documents.items():
                    bundle.writestr(name, body, compress_type=ZIP_DEFLATED)
                for name, path in paths.items():
                    bundle.write(path, arcname=name,
                                 compress_type=ZIP_STORED if name == "preview.mp4" else ZIP_DEFLATED)
        archive.seek(0)
        return archive
    except BaseException:
        archive.close()
        raise
