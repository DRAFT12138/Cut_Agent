"""open-webSearch 本地守护进程客户端。

守护进程（node tools/open-webSearch/build/index.js serve）提供：
  GET  /health            存活
  GET  /status            运行状态与配置
  POST /search            多引擎网页搜索（bing/baidu/duckduckgo/sogou/...）
  POST /fetch-web         抓取网页正文（request 优先，可选浏览器渲染）
  POST /fetch-github-readme / fetch-csdn / fetch-juejin / fetch-linuxdo

本模块是 cut_agent 联网探索的首选通道（比手写 Playwright 抓取更稳、
多引擎、结构化）。守护进程不可用时调用方应自行降级。

用法（命令行）：
  python -m cut_agent.websearch health
  python -m cut_agent.websearch search "免版权 背景音乐" --limit 5
  python -m cut_agent.websearch search "pexels video API" --engines bing,duckduckgo
  python -m cut_agent.websearch fetch "https://example.com"
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 默认端口与 config.py 中约定一致
DEFAULT_BASE_URL = os.environ.get("CUT_AGENT_OWS_BASE_URL", "http://127.0.0.1:3210")
TIMEOUT = int(os.environ.get("CUT_AGENT_OWS_TIMEOUT", "90"))


class WebSearchError(RuntimeError):
    pass


@dataclass
class SearchHit:
    title: str
    url: str
    description: str = ""
    source: str = ""
    engine: str = ""


@dataclass
class SearchResponse:
    query: str
    engines: list[str] = field(default_factory=list)
    total: int = 0
    results: list[SearchHit] = field(default_factory=list)
    partial_failures: list[dict] = field(default_factory=list)


class OpenWebSearch:
    """守护进程 HTTP 客户端；进程内可复用。"""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: int = TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 本地回环地址不走代理
        self._s = requests.Session()
        self._s.trust_env = False

    # ---------- 基础 ----------
    def health(self) -> bool:
        try:
            r = self._s.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200 and r.json().get("status") == "ok"
        except Exception:
            return False

    def status(self) -> dict:
        r = self._s.get(f"{self.base_url}/status", timeout=5)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise WebSearchError(f"status 异常: {data.get('error')}")
        return data.get("data") or {}

    def _post(self, path: str, body: dict) -> Any:
        try:
            r = self._s.post(f"{self.base_url}{path}", json=body, timeout=self.timeout)
        except requests.RequestException as e:
            raise WebSearchError(f"守护进程 {path} 请求失败（未启动？{e.__class__.__name__}）") from e
        if r.status_code != 200:
            raise WebSearchError(f"守护进程 {path} 返回 {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("status") != "ok":
            err = data.get("error") or {}
            raise WebSearchError(f"{path} 错误: {err.get('code')} {err.get('message')}")
        return data.get("data")

    # ---------- 能力 ----------
    def search(self, query: str, limit: int = 5,
               engines: Optional[list[str]] = None) -> SearchResponse:
        body: dict[str, Any] = {"query": query, "limit": limit}
        if engines:
            body["engines"] = engines
        d = self._post("/search", body) or {}
        hits = [SearchHit(title=x.get("title", ""), url=x.get("url", ""),
                          description=x.get("description", ""),
                          source=x.get("source", ""), engine=x.get("engine", ""))
                for x in d.get("results", [])]
        return SearchResponse(query=d.get("query", query),
                              engines=d.get("engines", []),
                              total=d.get("totalResults", len(hits)),
                              results=hits,
                              partial_failures=d.get("partialFailures", []) or [])

    def fetch_web(self, url: str, max_chars: int = 20000,
                  render_mode: str = "auto") -> dict:
        d = self._post("/fetch-web", {"url": url, "maxChars": max_chars,
                                      "renderMode": render_mode}) or {}
        return d

    def fetch_github_readme(self, url: str) -> str:
        d = self._post("/fetch-github-readme", {"url": url}) or {}
        return d.get("content", "")


# ---------- 进程内共享 ----------
_client: Optional[OpenWebSearch] = None


def client(base_url: str = DEFAULT_BASE_URL) -> OpenWebSearch:
    global _client
    if _client is None or _client.base_url != base_url.rstrip("/"):
        _client = OpenWebSearch(base_url)
    return _client


# ---------- CLI ----------
def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["health", "status", "search", "fetch", "readme"])
    ap.add_argument("arg", nargs="?", default="")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--engines", default="")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = ap.parse_args(argv)

    c = client(args.base_url)
    try:
        if args.cmd == "health":
            print(json.dumps({"ok": c.health()}, ensure_ascii=False))
        elif args.cmd == "status":
            print(json.dumps(c.status(), ensure_ascii=False))
        elif args.cmd == "search":
            engines = [e for e in args.engines.split(",") if e] or None
            resp = c.search(args.arg, limit=args.limit, engines=engines)
            print(json.dumps({"ok": True, "query": resp.query, "engines": resp.engines,
                              "total": resp.total,
                              "results": [h.__dict__ for h in resp.results]},
                             ensure_ascii=False, indent=1))
        elif args.cmd == "fetch":
            d = c.fetch_web(args.arg)
            print(json.dumps({"ok": True, "url": d.get("url"),
                              "content": (d.get("content") or "")[:20000]},
                             ensure_ascii=False, indent=1))
        elif args.cmd == "readme":
            print(c.fetch_github_readme(args.arg))
    except WebSearchError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
