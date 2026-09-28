"""浏览器/电脑控制层。

通过 Playwright 驱动*真实的 Chrome*（用户机器上的浏览器），完成联网搜索、
页面抓取、媒体下载与截屏。这是整个 agent 的"联网探索"手和眼。

设计要点：
- 复用本机 Chrome 可执行文件（config.CHROME_PATH），headless 运行，保留独立 profile。
- 每个公开函数都幂等、可重试；失败抛 BrowserError 而不是静默。
- 抓取结果统一为纯文本/URL/文件路径，便于上层 LLM 消费（本地模型无视觉，
  因此"看"网页靠的是渲染后的文本 + 结构化选择器，而非截屏识别）。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from .config import (CHROME_PATH, FETCH_TIMEOUT_MS, PROFILE_DIR,
                     SEARCH_TIMEOUT_MS, WORK_DIR)

try:
    from playwright.sync_api import (Browser, Playwright, sync_playwright,
                                     TimeoutError as PWTimeout)
    _PW_OK = True
except Exception:  # pragma: no cover - playwright 未装
    _PW_OK = False


class BrowserError(RuntimeError):
    pass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass
class MusicHit:
    title: str
    artist: str
    url: str
    page_url: str
    license: str = "CC0"


class BrowserController:
    """持有 Playwright 生命周期；线程内复用单浏览器。"""

    def __init__(self, headless: bool = True):
        if not _PW_OK:
            raise BrowserError("playwright 未安装，请先运行: playwright install chromium")
        self._headless = headless
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._page = None

    # ---------- 生命周期 ----------
    def start(self) -> "BrowserController":
        if self._browser is not None:
            return self
        if not Path(CHROME_PATH).exists():
            # 回退到 playwright 自带 chromium
            self._chrome = None
        else:
            self._chrome = CHROME_PATH
        self._pw = sync_playwright().start()
        try:
            if getattr(self, "_chrome", None):
                self._browser = self._pw.chromium.launch(
                    executable_path=self._chrome,
                    headless=self._headless,
                    args=["--no-first-run", "--no-default-browser-check",
                          "--disable-background-networking"],
                )
            else:
                self._browser = self._pw.chromium.launch(headless=self._headless)
        except Exception as e:
            self.stop()
            raise BrowserError(f"启动 Chrome 失败: {e}") from e
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        ctx = self._browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
            locale="zh-CN",
        )
        self._page = ctx.new_page()
        self._page.set_default_timeout(SEARCH_TIMEOUT_MS)
        return self

    def stop(self) -> None:
        try:
            if self._page is not None:
                self._page.context.close()
        except Exception:
            pass
        self._page = None
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        self._browser = None
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---------- 基础动作 ----------
    def goto(self, url: str, wait: str = "domcontentloaded") -> None:
        if self._page is None:
            self.start()
        try:
            self._page.goto(url, wait_until=wait, timeout=SEARCH_TIMEOUT_MS)
        except PWTimeout as e:
            raise BrowserError(f"打开页面超时: {url}") from e

    def page_text(self) -> str:
        if self._page is None:
            return ""
        try:
            return self._page.inner_text("body") or ""
        except Exception:
            return ""

    def screenshot(self, name: str) -> str:
        """截屏存到 work/，返回路径（用于留证/人工核查）。"""
        if self._page is None:
            raise BrowserError("页面未就绪")
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        p = WORK_DIR / f"screenshot_{name}.png"
        self._page.screenshot(path=str(p), full_page=False)
        return str(p)

    # ---------- 联网搜索 ----------
    def search(self, query: str, engine: str = "bing") -> list[SearchResult]:
        """在搜索引擎查询，返回前 N 条结果（title/url/snippet）。"""
        if engine == "bing":
            url = f"https://cn.bing.com/search?q={_urlenc(query)}"
        elif engine == "duckduckgo":
            url = f"https://duckduckgo.com/html/?q={_urlenc(query)}"
        else:
            url = f"https://www.bing.com/search?q={_urlenc(query)}"
        self.goto(url)
        time.sleep(1.5)  # 让广告/脚本落定
        soup = BeautifulSoup(self._page.content(), "html.parser")
        results: list[SearchResult] = []
        # bing 结果容器
        for li in soup.select("li.b_algo"):
            a = li.select_one("h2 a")
            if not a or not a.get("href"):
                continue
            txt = li.get_text(" ", strip=True)
            snippet = txt[:160]
            results.append(SearchResult(a.get_text(strip=True), a["href"], snippet))
        # duckduckgo 回退
        if not results:
            for a in soup.select("a.result__a"):
                href = a.get("href", "")
                results.append(SearchResult(a.get_text(strip=True), href, ""))
        # 去重
        seen = set()
        out = []
        for r in results:
            if r.url in seen or not r.url.startswith("http"):
                continue
            seen.add(r.url)
            out.append(r)
        return out[:10]

    # ---------- 页面抓取 ----------
    def fetch(self, url: str) -> str:
        """抓取页面正文文本（去掉脚本/样式/导航）。"""
        self.goto(url, wait="domcontentloaded")
        soup = BeautifulSoup(self._page.content(), "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.body or soup
        text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
        return text

    # ---------- 媒体下载 ----------
    def download(self, url: str, dest: Path, referer: str = "") -> Path:
        """下载二进制到 dest。优先 requests（带 referer），失败则回退浏览器 fetch。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")}
        if referer:
            headers["Referer"] = referer
        import requests as rq
        try:
            with rq.get(url, headers=headers, timeout=60, stream=True) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(64 * 1024):
                        f.write(chunk)
            if dest.stat().st_size > 1024:
                return dest
        except Exception:
            pass
        # 回退：浏览器 fetch → base64
        b64 = self._page.evaluate(
            """async (u) => {
                 const r = await fetch(u);
                 const b = await r.arrayBuffer();
                 let s=''; const bytes=new Uint8Array(b);
                 for (let i=0;i<bytes.length;i++) s+=String.fromCharCode(bytes[i]);
                 return btoa(s);
               }""",
            url,
        )
        import base64
        data = base64.b64decode(b64)
        dest.write_bytes(data)
        return dest

    # ---------- 素材探索 ----------
    def find_images(self, query: str, count: int = 6) -> list[dict]:
        """用 Bing 图片搜索找可商用图片，返回 [{title,url,page_url}]。"""
        self.goto(f"https://cn.bing.com/images/search?q={_urlenc(query)}&form=HDRSC2")
        time.sleep(2)
        out = []
        seen = set()
        # m 元素含 murl（原图）
        for m in self._page.eval_on_selector_all(
            "div.isv-r",
            """els => els.map(e => {
                   const m = e.querySelector('.m') || e;
                   try { const d = JSON.parse(m.getAttribute('m') || '{}');
                        return {url: d.murl, title: d.t || ''}; } catch(_) { return null; }
               })""",
        ):
            if not m or not m.get("url") or m["url"] in seen:
                continue
            seen.add(m["url"])
            out.append({"title": m.get("title", ""), "url": m["url"],
                        "page_url": ""})
            if len(out) >= count:
                break
        return out

    def find_music(self, mood: str, count: int = 4) -> list[MusicHit]:
        """在免版权音乐站找 BGM。优先 pixabay/直链，返回 [{title,artist,url,page_url}]。"""
        out: list[MusicHit] = []
        q = _urlenc(f"{mood} music royalty free download")
        # 1) Bing 搜索找直链 mp3
        for res in self.search(f"{mood} background music mp3 free download", engine="bing")[:8]:
            if re.search(r"\.(mp3|m4a)$", res.url, re.I):
                out.append(MusicHit(res.title, "", res.url, ""))
            elif re.search(r"(pixabay|freepd|incompetech|chosic)", res.url, re.I):
                out.append(MusicHit(res.title, "", "", res.url))
            if len(out) >= count:
                break
        # 2) 回退：freepd 直链目录（稳定、CC0）
        if len(out) < count:
            try:
                self.goto("https://freepd.com/")
                soup = BeautifulSoup(self._page.content(), "html.parser")
                base = "https://freepd.com"
                for a in soup.select("a[href*='.mp3']"):
                    href = a.get("href", "")
                    full = href if href.startswith("http") else base + href
                    out.append(MusicHit(a.get_text(strip=True) or full.rsplit("/", 1)[-1],
                                        "FreePD", full, ""))
                    if len(out) >= count:
                        break
            except Exception:
                pass
        # 去重
        seen = set(); dedup = []
        for h in out:
            key = h.url or h.page_url
            if not key or key in seen:
                continue
            seen.add(key)
            dedup.append(h)
        return dedup[:count]


def _urlenc(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


_global = BrowserController()


def controller() -> BrowserController:
    """进程内共享的浏览器控制器。"""
    return _global
