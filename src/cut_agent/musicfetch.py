"""免版权 BGM 抓取层（freepd.cn，CC0/公共领域）。

背景：原 freepd.com 已于 2026-01 关站（旧 browser.find_music 的回退是死链）。
freepd.cn 为其镜像，已实测可用：
- GET /api/music/categories          -> {"categories":[{"key","name","count"}]}
- GET /music（SSR 首页）              -> Comedy 分类 46 首曲目直链
    <a href="/api/music/<hex文件名>.mp3" download="Title.mp3">
- GET /api/music/<hex文件名>.mp3      -> audio/mpeg 直链可下载
其它分类的曲目列表接口（/api/music/<key>）当前 502，模块内做了尝试+降级：
任何拿不到的分类都会回落到 SSR 可解析的 Comedy 曲目。

设计：纯 requests、不走代理（.cn 站点直连即可）；每个函数幂等、失败降级不抛。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

BASE = "https://freepd.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 30


class MusicFetchError(RuntimeError):
    pass


@dataclass
class Track:
    title: str          # "Comedy/Alls Fair In Love.mp3"
    url: str            # 直链
    category: str = ""  # 分类 key


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 不走系统代理
    s.headers["User-Agent"] = UA
    return s


def categories() -> list[dict]:
    """情绪分类列表：[{key,name,count}]。失败返回内置兜底表。"""
    fallback = [
        {"key": "Comedy", "name": "诙谐搞怪", "count": 46},
        {"key": "Electronic", "name": "电子", "count": 14},
        {"key": "Epic", "name": "史诗/电影感", "count": 36},
        {"key": "Horror", "name": "恐怖悬疑", "count": 22},
        {"key": "Miscellaneous", "name": "综合", "count": 107},
        {"key": "Romance", "name": "浪漫温情", "count": 21},
        {"key": "Scoring", "name": "配乐", "count": 21},
        {"key": "Upbeat", "name": "欢快节奏", "count": 18},
        {"key": "World", "name": "世界民族", "count": 29},
        {"key": "Zoned", "name": "氛围/专注", "count": 732},
    ]
    try:
        s = _session()
        r = s.get(f"{BASE}/api/music/categories", timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json().get("categories") or []
        if data:
            return data
    except Exception:
        pass
    return fallback


def mood_to_category(mood: str) -> str:
    """把 LLM 给出的中文/英文情绪词映射到 freepd 分类 key。"""
    m = (mood or "").lower()
    table = [
        (("搞笑", "幽默", "诙谐", "滑稽", "喜剧", "逗", "comedy", "funny", "humor"), "Comedy"),
        (("史诗", "电影", "大片", "壮阔", "震撼", "epic", "cinematic", "movie", "trailer", "grand"), "Epic"),
        (("欢快", "轻快", "活泼", "快乐", "upbeat", "happy", "cheerful", "bright", "energetic"), "Upbeat"),
        (("浪漫", "温情", "温柔", "抒情", "romance", "romantic", "tender", "warm", "sweet"), "Romance"),
        (("电子", "电音", "电子乐", "科技", "future", "electronic", "edm", "synth", "digital"), "Electronic"),
        (("恐怖", "悬疑", "惊悚", "紧张", "horror", "suspense", "tension", "mystery", "dark"), "Horror"),
        (("氛围", "安静", "专注", "冥想", "lofi", "ambient", "calm", "focus", "zen", "chill"), "Zoned"),
        (("民族", "世界", "异域", "folk", "world", "ethnic"), "World"),
        (("配乐", "背景", "中性", "通用", "scoring", "score", "underscore"), "Scoring"),
    ]
    for kws, key in table:
        for kw in kws:
            if kw in m:
                return key
    return "Miscellaneous"


def _parse_ssr_tracks(html: str) -> list[Track]:
    """从 /music SSR HTML 解析曲目（含 download 属性里的真实标题）。"""
    out: list[Track] = []
    for href, hx, _dl in re.findall(
            r'href="(/api/music/([0-9a-f]+))"[^>]*download="([^"]+\.mp3)"', html):
        try:
            full = bytes.fromhex(hx).decode("utf-8")
        except Exception:
            full = _dl
        cat = full.split("/")[0] if "/" in full else ""
        out.append(Track(title=full, url=BASE + href, category=cat))
    return out


def _try_category_route(s: requests.Session, key: str) -> list[Track]:
    """尝试 /api/music/<key> 分类曲目接口（当前多数 502，能成则用）。"""
    try:
        r = s.get(f"{BASE}/api/music/{key}", timeout=TIMEOUT)
        if r.status_code != 200 or "json" not in r.headers.get("Content-Type", ""):
            return []
        data = r.json()
        # 兼容 {tracks:[...]} / [...] 两种形态
        rows = data.get("tracks") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = row.get("url") or row.get("path") or ""
            title = row.get("title") or row.get("name") or ""
            if not url:
                continue
            if not url.startswith("http"):
                url = BASE + url
            out.append(Track(title=title or url.rsplit("/", 1)[-1],
                             url=url, category=key))
        return out
    except Exception:
        return []


def tracks(category: str = "Miscellaneous", limit: int = 20) -> list[Track]:
    """取某分类曲目直链。分类接口不可用时回落到 SSR Comedy 曲目。"""
    s = _session()
    got = _try_category_route(s, category)
    if not got:
        try:
            html = s.get(f"{BASE}/music", timeout=TIMEOUT).text
            got = _parse_ssr_tracks(html)
        except Exception:
            return []
    # 若请求的是 Comedy，SSR 结果即本分类；否则尽量过滤（过滤后为空则保留全集兜底）
    if category and category != "Comedy":
        filtered = [t for t in got if t.category == category or category in t.title]
        if filtered:
            got = filtered
    return got[:limit]


def download(track: Track, dest_dir: Path) -> Path:
    """下载一首 mp3 到 dest_dir，返回本地路径（>50KB 才算有效）。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w\-\u4e00-\u9fff ]+", "_",
                  track.title.replace("/", "-"))[:60]
    dest = dest_dir / (name + ".mp3")
    s = _session()
    with s.get(track.url, timeout=120, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                f.write(chunk)
    if dest.stat().st_size < 50 * 1024:
        dest.unlink(missing_ok=True)
        raise MusicFetchError(f"下载内容过小（可能是错误页）: {track.url}")
    return dest


def download_for_mood(mood: str, dest_dir: Path, count: int = 1,
                      max_tries: int = 4) -> list[dict]:
    """按情绪下载 BGM。返回 [{title,url,local,size_kb,category}]。
    失败全部吞掉（返回已成功的部分），由调用方决定是否再降级。"""
    key = mood_to_category(mood)
    hits: list[dict] = []
    cands: Optional[list[Track]] = None
    # 先试目标分类，再试综合，最后 Comedy（SSR 保底）
    for cat in dict.fromkeys([key, "Miscellaneous", "Comedy"]):
        if len(hits) >= count:
            break
        if cands is None or True:
            try:
                cands = tracks(cat, limit=max_tries * 2)
            except Exception:
                cands = []
        for t in cands:
            if len(hits) >= count:
                break
            try:
                p = download(t, dest_dir)
                hits.append({"title": t.title, "url": t.url,
                             "local": str(p), "size_kb": p.stat().st_size // 1024,
                             "category": cat, "license": "CC0 (Public Domain)"})
            except Exception:
                continue
    return hits
