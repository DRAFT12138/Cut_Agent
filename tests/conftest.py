"""pytest 公共夹具（P8 测试基座）。

假 LLM（monkeypatch chat_json）：plan/match/music 返回预制 JSON，
可注入"失败"以覆盖降级路径。runs 根指到 tmp_path，不污染 output/。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cut_agent import graph, runctl
from cut_agent.llm import LLMError

SAMPLE_MEDIA = Path(__file__).resolve().parent.parent / "sample_media"
COPY = (
    "清晨的城市还在沉睡，第一缕光打在河面上。"
    "镜头穿过早高峰的车流，穿过写字楼的玻璃幕墙。"
    "午后，人群在广场上散去，有人跑步，有人发呆。"
    "夜幕落下，霓虹一盏盏亮起，这座城市从不真正睡着。"
)


def _plan_json() -> dict:
    return {"segments": [
        {"text": "清晨的城市还在沉睡，第一缕光打在河面上。", "mood": "安静 开场",
         "duration": 6, "kw_cn": ["清晨 河流"], "kw_en": ["morning river light"]},
        {"text": "镜头穿过早高峰的车流，穿过写字楼的玻璃幕墙。", "mood": "紧凑 发展",
         "duration": 6, "kw_cn": ["早高峰 车流"], "kw_en": ["city traffic morning"]},
        {"text": "午后，人群在广场上散去，有人跑步，有人发呆。", "mood": "松弛 高潮前",
         "duration": 6, "kw_cn": ["广场 人群"], "kw_en": ["plaza people afternoon"]},
        {"text": "夜幕落下，霓虹一盏盏亮起，这座城市从不真正睡着。", "mood": "情绪 收尾",
         "duration": 6, "kw_cn": ["夜景 霓虹"], "kw_en": ["city night neon"]},
    ]}


def _match_json(n_media: int) -> dict:
    # 必须引用 make_sample.py 生成的真实素材名
    names = ["city_night.mp4", "mountain_day.mp4", "ocean_waves.mp4"][: max(1, n_media)]
    rows = []
    for i in range(1, 5):
        rows.append({
            "seq": i, "media": names[i % len(names)],
            "kind": "video", "source": "local", "use_duration": 5.0,
            "start_offset": 0.0, "segment_text": f"段{i}", "needs_web": False,
        })
    return {"timeline": rows}


def _music_json() -> dict:
    return {"mood": "cinematic",
            "primary": {"title": "T-34 (mock)", "artist": "mock", "reason": "测试"},
            "alternatives": [{"title": "Alt (mock)", "artist": "mock", "reason": "测试"}]}


class FakeLLM:
    """chat_json 替身：按 prompt 特征分发预制 JSON；fail_plan 等开关触发降级。"""

    def __init__(self):
        self.calls: list[str] = []
        self.fail_plan = False
        self.fail_match = False
        self.fail_music = False
        self.seeds: list[int | None] = []

    def __call__(self, system: str, user: str, **kw) -> dict:
        from cut_agent.llm import LLMError
        self.calls.append(system[:60])
        self.seeds.append(kw.get("seed"))
        if "分镜师" in system or "切分" in system:
            if self.fail_plan:
                raise LLMError("fake plan failure")
            return _plan_json()
        if "时间线编排" in system:
            if self.fail_match:
                raise LLMError("fake match failure")
            return _match_json(3)
        if "配乐" in system:
            if self.fail_music:
                raise LLMError("fake music failure")
            return _music_json()
        return {}


@pytest.fixture
def tmp_runs(tmp_path: Path):
    """runs 根 → tmp_path/runs。"""
    runctl.set_runs_root_for_test(tmp_path / "runs")
    yield tmp_path / "runs"
    runctl.set_runs_root_for_test(None)


@pytest.fixture
def fake_llm(monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr(graph, "chat_json", f)
    monkeypatch.setattr(graph, "LLMError", __import__("cut_agent.llm", fromlist=["LLMError"]).LLMError)
    return f


@pytest.fixture(autouse=True)
def no_vision(monkeypatch):
    monkeypatch.setattr(graph, "VISION_ENABLED", False)


@pytest.fixture(autouse=True)
def no_music_download(monkeypatch):
    """pick_music 的 freepd 下载直接失败（走降级，不真下载）。"""
    from cut_agent.llm import LLMError
    def _fail(*a, **k):
        raise LLMError("fake: no network music")
    monkeypatch.setattr(graph, "download_for_mood", _fail)


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    """explore_web 走 ows 客户端失败 → 跳过（无时间线 needs_web 时该节点很快）。"""
    class _DeadClient:
        def health(self):
            return False
    monkeypatch.setattr(graph, "ows_client", lambda: _DeadClient())
    # playwright 路径也封掉（无网络测试不真起浏览器）
    def _no_browser(*a, **k):
        raise RuntimeError("fake: browser disabled in tests")
    monkeypatch.setattr(graph, "controller", _no_browser)


@pytest.fixture(autouse=True)
def fake_media(monkeypatch):
    """生命周期测试不依赖被 git 忽略的样片和 ffmpeg。"""
    from cut_agent.media import MediaItem
    monkeypatch.setattr(graph, "probe_media", lambda folder, **kwargs: [
        MediaItem(name=n, path=folder / n, kind="video", duration=8,
                  width=1280, height=720, fps=25)
        for n in ("city_night.mp4", "mountain_day.mp4", "ocean_waves.mp4")
    ])


@pytest.fixture(autouse=True)
def no_web_download(monkeypatch):
    monkeypatch.setattr(graph, "acquire_web", lambda asset, *a, **kw:
                        {**asset, "status": "manual", "error": "offline test"})


def wait_done(handle, timeout: float = 120.0):
    """等 run 线程到达终态。"""
    from cut_agent import runs
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = runs.status_of(handle.run_id).get("status")
        if st in ("done", "failed", "canceled", "paused"):
            handle.wait(5)
            return st
        time.sleep(0.2)
    raise TimeoutError(f"run {handle.run_id} 未在 {timeout}s 内结束")
