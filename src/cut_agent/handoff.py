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
_DOCS = ("精剪指导.md", "精剪核对记录.md", "粗剪方案.md", "精剪交接.html")
_FILES = ("plan.json", "cut_lines.txt")


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


def build(run_id: str) -> SpooledTemporaryFile:
    """Return a seeked ZIP; the caller owns and must close the file."""
    status = runs.status_of(run_id)
    if status["status"] != "done" or status.get("export_pending"):
        raise runctl.RunError("任务尚未完成或编辑导出正在恢复，暂不能生成交接包")
    root = runctl.run_dir(run_id)
    archive = SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    try:
        with runctl.RunLease(run_id):
            current = runctl.read_json(root / "run.json")
            if (not isinstance(current, dict) or current.get("status") != "done"
                    or exports.pending(root)):
                raise runctl.RunError("任务状态已变化，请稍后重新下载交接包")
            plan = load_plan(run_id)
            documents = {
                "精剪指导.md": _file(root, "精剪指导.md").read_text(encoding="utf-8"),
                "精剪核对记录.md": export_markdown(run_id),
                "粗剪方案.md": _file(root, "粗剪方案.md").read_text(encoding="utf-8"),
            }
            assets = _local_links(documents)
            paths = {name: _file(root, name) for name in (*_FILES, *sorted(assets))}
            source_dir = plan.get("media_folder") or "未记录"
            readme = ("# 精剪交接说明\n\n"
                      f"任务：{run_id}；方案修订：{plan.get('revision', 0)}。\n\n"
                      "先阅读《粗剪方案.md》中的时间线与源入出点，再按《精剪指导.md》或"
                      "《精剪核对记录.md》在达芬奇等剪辑软件中执行精剪。"
                      "也可直接在浏览器打开《精剪交接.html》离线查看三份文档与配图。"
                      "《plan.json》和《cut_lines.txt》可用于核对时间线或供后续工具读取；"
                      "执行清单每行对应一段画面，并列出源入出点、成片入出点及素材状态。\n\n"
                      "配图与文档保持原有相对路径；若方案中链接了粗剪预览，预览也已打包。"
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
