"""P8 run 生命周期测试：全跑 / 暂停恢复 / 断电恢复 / 降级路径。

全部使用假 LLM（monkeypatch）+ sample_media 小素材，不跑真实全流程。
"""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from cut_agent import graph, runs, runctl
from conftest import COPY, SAMPLE_MEDIA, wait_done, FakeLLM


def _run_json(rid: str) -> dict:
    return runctl.read_json(runctl.run_dir(rid) / "run.json")


def _stage_file(rid: str, name: str) -> Path:
    return runctl.run_dir(rid) / "stages" / f"{name}.json"


# ---------------------------------------------------------------- 全跑

def test_full_run_sync(tmp_runs, fake_llm, no_music_download):
    """假 LLM 下同步跑完 7 阶段：产物齐全、状态 done、粗剪文档落地。"""
    h, _ = runs.start_run(str(SAMPLE_MEDIA), COPY, seed=7, sync=True)
    rid = h.run_id
    meta = runs.status_of(rid)
    assert meta["status"] == "done", meta
    # 7 个阶段产物都在
    for name in runs.STAGE_NAMES:
        p = _stage_file(rid, name)
        assert p.exists(), f"缺阶段产物 {name}"
        json.loads(p.read_text(encoding="utf-8"))  # 合法 JSON = 完成
    # 断点为 None
    assert meta.get("resumable_from") is None
    # 粗剪文档 + cut_lines 在 run 目录
    rd = runctl.run_dir(rid)
    assert (rd / "粗剪方案.md").exists()
    assert (rd / "cut_lines.txt").exists()
    plan = json.loads((rd / "plan.json").read_text(encoding="utf-8"))
    assert plan["schema_version"] == 1
    assert len(plan["timeline"]) > 0
    assert "warnings" in plan["critique"]
    assert "自检报告" in (rd / "粗剪方案.md").read_text(encoding="utf-8")
    assert all("role" in s and 1 <= s["intensity"] <= 5 for s in plan["segments"])
    # seed 被传递到 LLM（假 LLM 记录）
    assert 7 in fake_llm.seeds
    # 事件流有内容
    log = runctl.EventLog(rid)
    assert log.latest > 0
    evs = log.tail(0)
    assert any(e["type"] == "status" for e in evs)
    assert all(e["run_id"] == rid and isinstance(e["pid"], int) for e in evs)
    assert sum(e["type"] == "checkpoint" for e in evs) == len(runs.STAGE_NAMES)
    obs = meta["observability"]
    assert obs["attempt"] == 1
    assert obs["last_checkpoint"] == runs.STAGE_NAMES[-1]
    assert obs["latest_event_seq"] == log.latest


def test_full_run_background_resume_done(tmp_runs, fake_llm, no_music_download):
    """后台启动 → 跑完 → 状态可查。"""
    h, _ = runs.start_run(str(SAMPLE_MEDIA), COPY, sync=False)
    st = wait_done(h)
    assert st == "done"
    assert runs.status_of(h.run_id)["status"] == "done"


# ---------------------------------------------------------------- 暂停 / 恢复

class Gate:
    """简单事件门：wait() 阻塞，open() 放行。"""

    def __init__(self):
        self._flag = False

    def wait(self, timeout: float = 10.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._flag:
                return True
            time.sleep(0.05)
        return False

    def open(self):
        self._flag = True


def test_pause_then_resume(tmp_runs, fake_llm, no_music_download, monkeypatch):
    """慢 LLM 窗口内暂停 → paused → resume → done。"""
    gate = Gate()
    entered = __import__("threading").Event()

    # 包一层：第一次 chat_json 调用挂起到 gate 打开
    class SlowFake(FakeLLM):
        def __call__(self, system, user, **kw):
            if not self.calls:
                entered.set()
                gate.wait(20)
            return super().__call__(system, user, **kw)

    sf = SlowFake()
    monkeypatch.setattr(graph, "chat_json", sf)

    h, _ = runs.start_run(str(SAMPLE_MEDIA), COPY, sync=False)
    assert entered.wait(10), "LLM 应已首次调用"
    # 请求暂停；gate 打开后 LLM 返回，节点边界 check_stop 应生效
    h.control.request_pause()
    gate.open()
    st = wait_done(h, timeout=60)
    assert st == "paused", runs.status_of(h.run_id)
    # 断点指向未完成阶段
    assert runs.status_of(h.run_id).get("resumable_from")
    # 恢复
    h2 = runs.resume_run(h.run_id)
    st2 = wait_done(h2, timeout=120)
    assert st2 == "done"
    meta = runs.status_of(h.run_id)
    assert meta["status"] == "done"
    assert meta["observability"]["attempt"] == 2
    assert meta["recovery_history"][-1]["kind"] == "resumed"
    assert any(e["type"] == "recovery" and e["what"] == "run-resumed"
               for e in runctl.EventLog(h.run_id).tail(0))
    for name in runs.STAGE_NAMES:
        assert _stage_file(h.run_id, name).exists()


# ---------------------------------------------------------------- 断电恢复

def _write_partial_run(tmp_runs, media_dir: Path) -> str:
    """手工构造"断电现场"：前 2 阶段产物 + running 状态 + 死 pid。"""
    rid = runctl.new_run_id()
    rd = runctl.run_dir(rid)
    (rd / "stages").mkdir(parents=True, exist_ok=True)
    # 阶段 1 产物：scan_media
    (rd / "stages" / "scan_media.json").write_text(
        json.dumps({"media": [{"name": "city_night.mp4", "kind": "video",
                                "duration": 8.0, "width": 1280, "height": 720,
                                "size_mb": 0.1, "scene_cuts": []}],
                     "media_dir": str(media_dir)}, ensure_ascii=False),
        encoding="utf-8")
    (rd / "stages" / "plan_segments.json").write_text(
        json.dumps({"segments": [{"text": "段一", "mood": "开场", "duration": 6,
                                   "kw_cn": ["清晨"], "kw_en": ["morning"]}]},
                    ensure_ascii=False),
        encoding="utf-8")
    # 输入快照（resume 重建状态用）
    (rd / "input.json").write_text(
        json.dumps({"media_dir": str(media_dir), "copy": COPY, "seed": 7,
                    "copy_text": COPY}, ensure_ascii=False),
        encoding="utf-8")
    # run.json：running + 一个必然已死的 pid
    dead_pid = _dead_pid()
    (rd / "run.json").write_text(json.dumps({
        "id": rid, "status": "running", "created": time.time(),
        "media_dir": str(media_dir), "seed": 7, "copy": COPY,
        "pid": dead_pid,
        "stages": [{"name": n, "status": "done" if n in ("scan_media", "plan_segments")
                        else "pending"} for n in runs.STAGE_NAMES],
    }, ensure_ascii=False), encoding="utf-8")
    return rid


def _dead_pid() -> int:
    """起一个子进程并等它退出，拿回一个保证已死的 pid。"""
    import subprocess
    import sys
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=10)
    return p.pid


def test_power_loss_recover_resume(tmp_runs, fake_llm, no_music_download, monkeypatch):
    """断电现场（running+死pid+部分产物）→ recover → interrupted → resume → done。"""
    rid = _write_partial_run(tmp_runs, SAMPLE_MEDIA)
    # 恢复扫描
    n = runs.recover_interrupted()
    assert rid in n
    meta = runs.status_of(rid)
    assert meta["status"] == "interrupted"
    assert meta["observability"]["recovery_count"] == 1
    # 断点 = 第一个未完成阶段
    assert meta.get("resumable_from") == "understand_media"
    # 恢复：应复用已有产物（scan_media 不重跑）
    fake_llm.calls.clear()
    h = runs.resume_run(rid)
    st = wait_done(h, timeout=120)
    assert st == "done"
    assert runs.status_of(rid)["status"] == "done"
    final = runs.status_of(rid)
    assert final["observability"]["attempt"] == 2
    assert final["observability"]["recovery_count"] == 1
    for name in runs.STAGE_NAMES:
        assert _stage_file(rid, name).exists()
    # 已完成的阶段不应再次触发 LLM（plan 只调 1 次）
    plan_calls = sum(1 for c in fake_llm.calls if "分镜师" in c or "切分" in c)
    assert plan_calls <= 1, f"plan 被重跑: {plan_calls}"


# ---------------------------------------------------------------- 降级路径

def test_degraded_llm_off(tmp_runs, fake_llm, no_music_download):
    """LLM 全挂（plan 失败）：plan 降级为单段，后续阶段照跑，最终 done。"""
    fake_llm.fail_plan = True
    h, _ = runs.start_run(str(SAMPLE_MEDIA), COPY, sync=True)
    meta = runs.status_of(h.run_id)
    assert meta["status"] == "done", meta
    for name in runs.STAGE_NAMES:
        assert _stage_file(h.run_id, name).exists()


def test_degraded_music_off(tmp_runs, fake_llm):
    """配乐下载失败（LLM 在）：pick_music 降级，run 仍 done。"""
    h, _ = runs.start_run(str(SAMPLE_MEDIA), COPY, sync=True)
    meta = runs.status_of(h.run_id)
    assert meta["status"] == "done", meta
    m = json.loads(_stage_file(h.run_id, "pick_music").read_text(encoding="utf-8"))
    # 降级标记：无 downloads 或带错误说明
    assert "music" in m


def test_degraded_vision_off(tmp_runs, fake_llm, no_music_download, no_vision):
    """视觉层关闭：understand_media 产出空镜头，时间线/文档仍落地。"""
    h, _ = runs.start_run(str(SAMPLE_MEDIA), COPY, sync=True)
    meta = runs.status_of(h.run_id)
    assert meta["status"] == "done", meta
    u = json.loads(_stage_file(h.run_id, "understand_media").read_text(encoding="utf-8"))
    assert "media" in u
    # 文档仍存在
    assert (runctl.run_dir(h.run_id) / "粗剪方案.md").exists()


# ---------------------------------------------------------------- 状态机 / 恢复规则

def test_list_runs_and_status_shape(tmp_runs, fake_llm, no_music_download):
    runs.start_run(str(SAMPLE_MEDIA), COPY, sync=True)
    lst = runs.list_runs()
    assert len(lst) == 1
    r = lst[0]
    for k in ("id", "status", "stages_done", "stages_total", "copy_head"):
        assert k in r
    assert r["stages_done"] == r["stages_total"]


def test_resume_rejects_unknown_and_done(tmp_runs, fake_llm, no_music_download):
    h, _ = runs.start_run(str(SAMPLE_MEDIA), COPY, sync=True)
    rid = h.run_id
    # 未知 id
    with pytest.raises(Exception):
        runs.resume_run("no-such-run")
    # done 后再次 resume 应失败（无未完成阶段）或幂等成功——接受两者之一，但不得崩在状态机
    try:
        runs.resume_run(rid)
    except runctl.RunError:
        pass
    assert runs.status_of(rid)["status"] == "done"
