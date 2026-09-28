"""Recoverable publication of a fully rendered, offline edit.

The pending manifest is the read barrier. Payload files remain available for
idempotent replay until every export, stage, plan and metadata file is replaced.
Call mutations only while holding the run's worker lease.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from . import runctl

MANIFEST = ".export-pending.json"


def pending(root: Path) -> bool:
    return (root / MANIFEST).exists()


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def discard_staging(root: Path, staging: Path) -> None:
    root, staging = root.resolve(), staging.resolve()
    if staging.parent != root or not staging.name.startswith(".rebuild-"):
        raise runctl.RunError("编辑暂存目录超出任务范围")
    shutil.rmtree(staging)


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as content:
            shutil.copyfileobj(content, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def publish(root: Path, staging: Path, revision: int) -> None:
    root, staging = root.resolve(), staging.resolve()
    if pending(root):
        raise runctl.RunError("上次编辑导出尚未恢复")
    if staging.parent != root or not staging.name.startswith(".rebuild-"):
        raise runctl.RunError("编辑暂存目录超出任务范围")
    files = []
    for path in sorted(staging.rglob("*")):
        if path.is_file():
            # Rendering libraries close their files; flush payloads before the
            # durable manifest makes recovery responsible for this revision.
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())
            files.append({"path": path.relative_to(staging).as_posix(), "sha256": _digest(path)})
    runctl.write_json_atomic(root / MANIFEST, {
        "version": 1, "staging": staging.name, "revision": revision, "files": files})
    recover(root)


def recover(root: Path) -> bool:
    root = root.resolve()
    if not pending(root):
        return False
    record = runctl.read_json(root / MANIFEST)
    if not isinstance(record, dict) or record.get("version") != 1:
        raise runctl.RunError("编辑导出记录损坏，已保留文件待恢复")
    name = record.get("staging")
    if not isinstance(name, str) or Path(name).name != name or not name.startswith(".rebuild-"):
        raise runctl.RunError("编辑暂存目录无效")
    staging = (root / name).resolve()
    if staging.parent != root:
        raise runctl.RunError("编辑暂存目录超出任务范围")
    entries = record.get("files")
    if not isinstance(entries, list) or not entries:
        raise runctl.RunError("编辑导出清单为空")
    checked = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise runctl.RunError("编辑导出清单损坏")
        relative = Path(entry["path"])
        source, target = (staging / relative).resolve(), (root / relative).resolve()
        if (not relative.parts or relative.is_absolute() or ".." in relative.parts or not source.is_relative_to(staging)
                or not target.is_relative_to(root) or relative.parts[0].startswith(".")):
            raise runctl.RunError("编辑导出路径无效")
        if not source.is_file() or _digest(source) != entry.get("sha256"):
            raise runctl.RunError(f"编辑暂存文件缺失或损坏：{entry['path']}")
        checked.append((source, target))
    names = {target.relative_to(root).as_posix() for _, target in checked}
    required = {"plan.json", "plan.md", "run.json", "粗剪方案.md", "cut_lines.txt", "精剪指导.md"}
    required.update(f"stages/{name}.json" for name in (
        "scan_media", "plan_segments", "understand_media", "build_timeline", "explore_web",
        "pick_music", "write_doc", "finishing_guide"))
    plan = runctl.read_json(staging / "plan.json")
    if not isinstance(plan, dict) or plan.get("schema_version") != 1 or plan.get("revision") != record.get("revision"):
        raise runctl.RunError("编辑计划与导出版本不一致")
    try:
        required.update(card["frame"] for card in plan["storyboard"]["cards"])
        required.update(cover["frame"] for cover in plan["finishing"]["covers"])
    except (KeyError, TypeError):
        raise runctl.RunError("编辑计划缺少分镜或指导文件清单")
    if (plan.get("preview") or {}).get("status") == "ready":
        required.add("preview.mp4")
    if not required <= names or len(names) != len(checked):
        raise runctl.RunError("编辑导出清单不完整或重复")
    # Check every payload before touching any destination. Retain all sources
    # during publication, even those already copied by a previous attempt.
    checked.sort(key=lambda pair: (2 if pair[1] == root / "run.json" else
                                   1 if pair[1] == root / "plan.json" else 0, str(pair[1])))
    for source, target in checked:
        _copy_atomic(source, target)
    (root / MANIFEST).unlink()
    try:
        discard_staging(root, staging)
    except OSError:
        pass  # The revision is committed; an orphaned private payload is harmless.
    return True
