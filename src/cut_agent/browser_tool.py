"""浏览器控制 CLI（供 Cordis 插件 / 其它进程调用）。

每个 action 输出 JSON 到 stdout：
  web_search  --query Q [--engines bing,baidu] [--count N]   （open-webSearch 守护进程多引擎搜索）
  web_fetch   --url U [--max-chars N]                        （open-webSearch 正文抓取）
  search      --query Q [--engine bing|duckduckgo]           （Playwright Bing/DDG）
  fetch       --url U            （正文文本 + 截屏路径）
  screenshot  --url U [--name N]
  find_images --query Q [--count N]
  find_music  --mood M [--count N]   （freepd.cn 按情绪 CC0 直链下载优先）
  download    --url U --dest P [--referer R]
  cut         --media DIR --copy FILE|TEXT   （跑完整粗剪流水线）
  probe       --media DIR    （只扫描本地素材）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 本文件位于 <root>/src/cut_agent/browser_tool.py
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _out(payload: dict) -> None:
    # Windows 控制台默认 GBK，强制 UTF-8 避免 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", required=True,
                    choices=["web_search", "web_fetch", "search", "fetch", "screenshot",
                             "find_images", "find_music", "download", "cut", "probe"])
    ap.add_argument("--query", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--engine", default="bing")
    ap.add_argument("--engines", default="", help="web_search 用，逗号分隔多引擎")
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--max-chars", type=int, default=20000)
    ap.add_argument("--mood", default="cinematic")
    ap.add_argument("--dest", default="")
    ap.add_argument("--referer", default="")
    ap.add_argument("--name", default="")
    ap.add_argument("--media", default="")
    ap.add_argument("--copy", default="")
    args = ap.parse_args(argv)

    try:
        if args.action == "web_search":
            from cut_agent.websearch import WebSearchError, client as ows
            try:
                c = ows()
                if not c.health():
                    raise WebSearchError("open-webSearch 守护进程未启动")
                engines = [e for e in args.engines.split(",") if e] or None
                resp = c.search(args.query, limit=max(1, args.count), engines=engines)
                _out({"ok": True, "query": resp.query, "engines": resp.engines,
                      "total": resp.total, "results": [h.__dict__ for h in resp.results]})
            except WebSearchError as e:
                _out({"ok": False, "error": str(e)})
                return 1

        elif args.action == "web_fetch":
            from cut_agent.websearch import WebSearchError, client as ows
            try:
                c = ows()
                if not c.health():
                    raise WebSearchError("open-webSearch 守护进程未启动")
                d = c.fetch_web(args.url, max_chars=max(1000, args.max_chars))
                text = d.get("content") or ""
                _out({"ok": True, "url": d.get("url"), "method": d.get("retrievalMethod"),
                      "text": text, "truncated": bool(d.get("truncated"))})
            except WebSearchError as e:
                _out({"ok": False, "error": str(e)})
                return 1

        elif args.action == "find_music":
            from cut_agent.musicfetch import download_for_mood
            dest_dir = Path(args.dest) if args.dest else (ROOT / "work" / "music")
            hits = download_for_mood(args.mood, dest_dir, count=args.count, max_tries=4)
            _out({"ok": True, "mood": args.mood, "music": hits, "count": len(hits)})

        elif args.action == "search":
            from cut_agent.browser import controller
            bc = controller().start()
            results = bc.search(args.query, engine=args.engine)
            bc.stop()
            _out({"ok": True, "results": [r.__dict__ for r in results]})

        elif args.action == "fetch":
            from cut_agent.browser import controller
            bc = controller().start()
            text = bc.fetch(args.url)
            shot = ""
            try:
                shot = bc.screenshot("fetch")
            except Exception:
                pass
            bc.stop()
            _out({"ok": True, "url": args.url, "screenshot": shot,
                  "text": text[:20000], "truncated": len(text) > 20000})

        elif args.action == "screenshot":
            from cut_agent.browser import controller
            bc = controller().start()
            bc.goto(args.url)
            shot = bc.screenshot(args.name or "shot")
            bc.stop()
            _out({"ok": True, "screenshot": shot, "url": args.url})

        elif args.action == "find_images":
            from cut_agent.browser import controller
            bc = controller().start()
            imgs = bc.find_images(args.query, count=args.count)
            bc.stop()
            _out({"ok": True, "images": imgs})

        elif args.action == "download":
            from cut_agent.browser import controller
            dest = Path(args.dest)
            bc = controller().start()
            try:
                path = bc.download(args.url, dest, referer=args.referer)
            finally:
                bc.stop()
            _out({"ok": True, "path": str(path), "size": path.stat().st_size})

        elif args.action == "probe":
            from cut_agent.media import probe_media
            items = probe_media(Path(args.media))
            _out({"ok": True, "media": [
                {"name": i.name, "kind": i.kind, "duration": round(i.duration, 2),
                 "width": i.width, "height": i.height,
                 "scene_cuts": [round(c, 1) for c in i.scene_cuts]}
                for i in items]})

        elif args.action == "cut":
            from cut_agent.graph import run
            copy_src = args.copy
            p = Path(copy_src)
            copy = p.read_text(encoding="utf-8") if p.is_file() else copy_src
            state = run(args.media, copy)
            _out({"ok": True,
                  "doc_path": state.get("doc_path"),
                  "line_doc_path": state.get("line_doc_path"),
                  "timeline": state.get("timeline"),
                  "music": state.get("music"),
                  "web_assets": state.get("web_assets"),
                  "log": state.get("log")})
        return 0
    except Exception as e:  # noqa: BLE001
        _out({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
