"""命令行入口。

用法：
  python -m cut_agent.cli run --media <素材文件夹> --copy <文案文件.txt|字符串> [--seed N] [--sync]
  python -m cut_agent.cli resume <run_id>
  python -m cut_agent.cli status [run_id]
  python -m cut_agent.cli list
  python -m cut_agent.cli serve --port 8090          # Phase 0 Web 控制台

`run` 默认工作线程执行并轮询打印事件（Ctrl+C 请求暂停，可用 resume 续跑）；
`--sync` 前台阻塞跑完。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import runs
from .runctl import RunError, run_dir as runctl_run_dir


def _read_copy(src: str) -> str:
    p = Path(src)
    return p.read_text(encoding="utf-8") if p.is_file() else src


def cmd_run(args) -> int:
    copy = _read_copy(args.copy)
    media_folder = Path(args.media).resolve()
    if not media_folder.is_dir():
        print(f"素材目录不存在: {media_folder}", file=sys.stderr)
        return 2
    agent_decisions = None
    if args.agent_decisions:
        from .agent_tools import load_decisions
        from .config import WORK_DIR
        from .media import probe_media
        media = [{key: value for key, value in vars(item).items() if key != "path"}
                 for item in probe_media(media_folder, cache_dir=WORK_DIR / "media_cache")]
        agent_decisions = load_decisions(args.agent_decisions, media, copy)
    runs.recover_interrupted()
    handle, _ = runs.start_run(str(media_folder), copy, seed=args.seed,
                               options={"preview": args.preview, "platform": args.platform,
                                        "finishing_llm": not args.no_finishing_llm,
                                        "agent_decisions": agent_decisions or {}},
                               sync=args.sync)
    print(f"run: {handle.run_id}")
    if args.sync:
        meta = runs.status_of(handle.run_id)
        return _print_meta(meta)
    return _follow(handle.run_id, until_done=True)


def cmd_resume(args) -> int:
    runs.recover_interrupted()
    handle = runs.resume_run(args.run_id)
    print(f"恢复 run: {args.run_id}")
    return _follow(args.run_id, until_done=True)


def cmd_status(args) -> int:
    runs.recover_interrupted()
    for rid in ([args.run_id] if args.run_id else [r["id"] for r in runs.list_runs()]):
        meta = runs.status_of(rid)
        if _print_meta(meta):
            return 1
    return 0


def _print_meta(meta: dict) -> int:
    print(f"\nrun {meta['id']}  [{meta.get('status')}]")
    obs = meta.get("observability", {})
    if obs:
        checkpoint = obs.get("last_checkpoint") or "-"
        current = obs.get("current_stage") or "-"
        print(f"  尝试: {obs.get('attempt', 1)}  恢复: {obs.get('recovery_count', 0)}  "
              f"当前: {current}  最近断点: {checkpoint}  事件: {obs.get('latest_event_seq', 0)}")
    for s in meta.get("stages", []):
        mark = {"done": "✓", "degraded": "✓~", "running": "▶",
                "paused": "⏸", "failed": "✗", "pending": "·"}.get(s.get("status"), "?")
        extra = s.get("elapsed")
        extra = f"  ({extra}s)" if isinstance(extra, (int, float)) else ""
        err = f"  {s.get('error')}" if s.get("error") else ""
        print(f"  {mark} {s['name']}{extra}{err}")
    if meta.get("error"):
        print(f"  错误: {meta['error']}")
    return 1 if meta.get("status") == "failed" else 0


def cmd_list(args) -> int:
    runs.recover_interrupted()
    rs = runs.list_runs()
    if not rs:
        print("（无 run）")
        return 0
    for r in rs:
        print(f"{r['id']}  [{r['status']}]  {r['stages_done']}/{r['stages_total']}  {r['copy_head']}")
    return 0


def _events_since(run_id: str, since: int) -> list[dict]:
    """读 log.jsonl 中 seq>since 的事件（增量跟随）。"""
    from .runctl import EventLog
    return EventLog(run_id).tail(since)


def _follow(run_id: str, until_done: bool) -> int:
    """轮询事件流打印；run 到达终态后返回。

    Ctrl+C 请求协作暂停，等待当前单位结束并保存断点。
    """
    since = 0
    terminal = {"done", "failed", "canceled", "paused", "interrupted"}
    try:
        while True:
            events = _events_since(run_id, since)
            for e in events:
                since = e["seq"]
                if e["type"] == "status":
                    print(f"  [{e['stage']}] {e.get('what')}")
                elif e["type"] == "progress":
                    print(f"  [{e['stage']}] {e.get('done')}/{e.get('total')} {e.get('item','')}")
                else:
                    print(f"  [{e['stage']}] {e.get('msg','')}")
            if len(events) == 500:
                continue  # drain the final page before reporting the terminal state
            meta = runs.status_of(run_id)
            st = meta.get("status")
            if st in terminal:
                print(f"\nrun {run_id} → {st}")
                if st == "paused":
                    print("  已暂停：python -m cut_agent.cli resume " + run_id)
                return _print_meta(meta)
            if not until_done:
                return 0
            time.sleep(1.0)
    except KeyboardInterrupt:
        runs.pause_run(run_id)
        print("\n正在暂停：等待当前子任务结束并保存断点…")
        return _follow(run_id, until_done=True)


def cmd_serve(args) -> int:
    try:
        from .server import make_app
    except ImportError as e:
        print(f"Web 控制台未就绪: {e}", file=sys.stderr)
        return 3
    import uvicorn
    app = make_app()
    runs.recover_interrupted()
    print(f"Cut Agent 控制台: http://127.0.0.1:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_agent_context(args) -> int:
    """Export JSON that Codex/Claude Code can inspect without an LLM API."""
    from .agent_tools import build_context
    payload = build_context(args.media, _read_copy(args.copy))
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(rendered)
    else:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(output.resolve())
    return 0


def main(argv: list[str] | None = None) -> int:
    # argparse writes localized help before command dispatch.  Redirected
    # streams on Windows CI can default to cp1252, which cannot encode it.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass
    ap = argparse.ArgumentParser(description="Cut Agent 粗剪流水线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="新建并运行一个 run")
    p.add_argument("--media", required=True, help="素材文件夹路径")
    p.add_argument("--copy", required=True, help="文案：文件路径或内联文本")
    p.add_argument("--seed", type=int, default=None, help="LLM 随机种子（A/B 可复现）")
    p.add_argument("--sync", action="store_true", help="前台阻塞跑完（默认后台+跟随）")
    p.add_argument("--preview", action="store_true", help="额外生成带段号的预览片")
    p.add_argument("--platform", choices=["douyin", "xiaohongshu", "bilibili"], default="douyin")
    p.add_argument("--no-finishing-llm", action="store_true", help="精剪指导只用离线规则")
    p.add_argument("--agent-decisions", metavar="JSON",
                   help="使用 Codex/Claude Code 生成的 JSON 决策代替分段、编排和配乐模型调用")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("agent-context", help="为 Codex/Claude Code 导出素材、缩略图和决策契约 JSON")
    p.add_argument("--media", required=True, help="素材文件夹路径")
    p.add_argument("--copy", required=True, help="文案：文件路径或内联文本")
    p.add_argument("--output", default="agent-context.json", help="输出 JSON 路径；- 表示 stdout")
    p.set_defaults(fn=cmd_agent_context)

    p = sub.add_parser("resume", help="从断点恢复 run（暂停/断电/失败）")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("variant", help="同素材同文案生成另一版，缺省 seed 加一")
    p.add_argument("run_id")
    p.add_argument("--seed", type=int)
    p.set_defaults(fn=cmd_variant)

    p = sub.add_parser("status", help="查看 run 状态（缺省=全部）")
    p.add_argument("run_id", nargs="?")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("list", help="列出全部 run")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("serve", help="启动 Web 控制台（Phase 0）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("rebuild", help="从 plan.json 离线重建导出")
    p.add_argument("--run", required=True, help="run ID 或运行目录")
    p.set_defaults(fn=cmd_edit, edit=False)
    p.add_argument("--preview", action="store_true", help="同时重新生成预览")
    p = sub.add_parser("edit", help="离线编辑计划并重建导出（行号从 1 开始）")
    p.add_argument("--run", required=True)
    operations = p.add_mutually_exclusive_group(required=True)
    operations.add_argument("--move", nargs=2, type=int, metavar=("FROM", "TO"))
    operations.add_argument("--drop", type=int)
    operations.add_argument("--duration", nargs=2, metavar=("ROW", "SECONDS"))
    operations.add_argument("--swap", nargs=2, metavar=("ROW", "MEDIA"))
    operations.add_argument("--append", metavar="MEDIA")
    p.add_argument("--shot", type=int)
    p.set_defaults(fn=cmd_edit, edit=True)
    p.add_argument("--preview", action="store_true", help="同时重新生成预览")

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (RunError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def cmd_edit(args) -> int:
    from .editing import rebuild
    rid = args.run
    candidate = Path(rid)
    if candidate.is_dir():
        from .runctl import runs_root
        if candidate.resolve().parent != runs_root().resolve():
            raise RunError("运行目录必须位于当前 runs 根目录")
        rid = candidate.name
    operation = None
    if args.edit:
        if args.move:
            operation = {"op": "move", "row": args.move[0], "to": args.move[1]}
        elif args.drop is not None:
            operation = {"op": "drop", "row": args.drop}
        elif args.duration:
            operation = {"op": "duration", "row": int(args.duration[0]), "seconds": float(args.duration[1])}
        elif args.swap:
            operation = {"op": "swap", "row": int(args.swap[0]), "media": args.swap[1], "shot": args.shot}
        else:
            operation = {"op": "append", "media": args.append, "shot": args.shot}
    plan = rebuild(rid, operation, preview=args.preview)
    print(f"计划已重建，版本 {plan['revision']}：{plan['doc_path']}")
    return 0


def cmd_variant(args) -> int:
    handle, _ = runs.start_variant(args.run_id, args.seed)
    print(f"新版本 run: {handle.run_id}")
    return _follow(handle.run_id, until_done=True)


if __name__ == "__main__":
    raise SystemExit(main())
