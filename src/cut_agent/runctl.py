"""run 基础设施（P8）：run 目录、阶段产物原子读写、控制标志、事件流。

设计（见 CRAFT.md P8）：
- 每个 run 一个目录 output/runs/<run_id>/，阶段产物 stages/<名>.json 原子落盘，
  "有效 JSON 即该阶段完成"——断电判定不依赖 run.json 自身完整。
- 控制：协作式标志（pause/cancel），节点在检查点轮询。
- 事件流：log.jsonl 追加 {seq, ts, stage, type, ...}，前端 ?since= 增量轮询。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_CURRENT_CONTROL: ContextVar = ContextVar("cut_agent_control", default=None)


@contextmanager
def control_scope(control):
    """Keep retry cancellation local to the executing thread/context."""
    token = _CURRENT_CONTROL.set(control)
    try:
        yield
    finally:
        _CURRENT_CONTROL.reset(token)


def retry_checkpoint(delay: float = 0) -> None:
    control = _CURRENT_CONTROL.get()
    if control is None:
        if delay:
            time.sleep(delay)
        return
    if delay:
        control.wait_stop(delay)
    check_stop(control, "llm_retry")

# run 目录根：默认 output/runs（测试可指到 tmp）
_RUNS_ROOT_OVERRIDE: Path | None = None
_RUNS_ROOT_LOCK = threading.Lock()


def runs_root() -> Path:
    global _RUNS_ROOT_OVERRIDE
    if _RUNS_ROOT_OVERRIDE is not None:
        return _RUNS_ROOT_OVERRIDE
    from .config import OUTPUT_DIR
    d = OUTPUT_DIR / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_runs_root_for_test(p: Path | None) -> None:
    """测试用：把 runs 根指到临时目录（None 恢复默认）。"""
    global _RUNS_ROOT_OVERRIDE
    with _RUNS_ROOT_LOCK:
        _RUNS_ROOT_OVERRIDE = p
        if p is not None:
            p.mkdir(parents=True, exist_ok=True)


# ---------------- run 目录 ----------------

def new_run_id(ts: float | None = None) -> str:
    ts = ts if ts is not None else time.time()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(ts))
    rand = re.sub(r"[^a-z0-9]", "",
                  os.urandom(4).hex()[:4])
    return f"{stamp}-{rand}"


def run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id):
        raise RunError("无效 run ID")
    d = runs_root() / run_id
    return d


def stage_path(run_id: str, name: str) -> Path:
    return run_dir(run_id) / "stages" / f"{name}.json"


class RunLease:
    """OS-owned exclusive worker lock; automatically released on process death."""

    def __init__(self, run_id: str):
        path = run_dir(run_id) / ".worker.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RunError(f"run {run_id} 已有执行器或恢复操作，请稍后重试") from exc

    def close(self):
        if self.file.closed:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------- 原子 JSON 读写 ----------------

def write_json_atomic(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        # A Windows reader may briefly hold the destination without delete
        # sharing while the Web UI polls run.json. Keep the replacement atomic,
        # but allow a bounded window for that handle to close.
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep((0.02, 0.04, 0.08, 0.16)[attempt])
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(p: Path):
    for attempt in range(5):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except PermissionError:
            if attempt == 4:
                return None
            time.sleep((0.02, 0.04, 0.08, 0.16)[attempt])
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
    return None


def read_json_anywhere(*paths: Path):
    for p in paths:
        d = read_json(p)
        if d is not None:
            return d
    return None


# ---------------- 事件流 ----------------

def _event_records(path: Path):
    if not path.is_file():
        return
    # A power cut can leave a partial UTF-8 character or JSON record at EOF.
    with path.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            try:
                record = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and isinstance(record.get("seq"), int):
                yield record


@dataclass
class EventLog:
    run_id: str
    on_progress: Callable[[str, dict], None] | None = None
    progress: dict[str, dict] = field(default_factory=dict, init=False)
    _path: Path = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _seq: int = field(default=0, init=False)

    def __post_init__(self):
        self._path = run_dir(self.run_id) / "log.jsonl"
        # 续写：已有日志（断点恢复）从最后 seq+1 开始
        self._seq = max((r["seq"] for r in _event_records(self._path)), default=0)

    def event(self, stage: str, type_: str, **kw) -> None:
        """Durably append one structured event for live tailing and replay."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._seq += 1
            rec = {"seq": self._seq, "ts": round(time.time(), 3),
                   "run_id": self.run_id, "pid": os.getpid(),
                   "stage": stage, "type": type_, **kw}
            with self._path.open("a+b") as f:
                if f.tell():
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        f.write(b"\n")  # separate an interrupted record from the next event
                f.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            if type_ == "progress":
                self.progress[stage] = dict(kw)
                if self.on_progress:
                    self.on_progress(stage, dict(kw))

    def tail(self, since: int = 0, limit: int = 500) -> list[dict]:
        """seq > since 的事件（增量轮询用）。"""
        out: list[dict] = []
        for rec in _event_records(self._path):
            if rec["seq"] > since:
                out.append(rec)
                if len(out) >= limit:
                    break
        return out

    @property
    def latest(self) -> int:
        """当前最大 seq（0=空日志）。"""
        return self._seq


# ---------------- 控制标志 ----------------

@dataclass
class RunControl:
    """协作式控制：pause/cancel 标志。节点在检查点轮询。

    - paused：在飞子任务跑完当前单位后停（检查点生效）；
    - canceled：同 paused 生效时机，但停后 run 置 canceled（产物保留，可恢复）。
    幂等：重复 request_pause 不覆盖已设置的 cancel。
    """
    run_id: str
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _canceled: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def request_pause(self) -> None:
        with self._lock:
            self._stop.set()

    def request_cancel(self) -> None:
        with self._lock:
            self._canceled = True
            self._stop.set()

    def request_resume(self) -> None:
        with self._lock:
            if not self._canceled:
                self._stop.clear()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def canceled(self) -> bool:
        with self._lock:
            return self._canceled

    def wait_stop(self, timeout: float | None = None) -> bool:
        """阻塞等待 stop 被请求（长任务内部轮询用）。True=被请求停止。"""
        return self._stop.wait(timeout)


class RunHalted(RuntimeError):
    """run 被暂停/取消：驱动循环捕获后持久化状态并退出。

    属性: kind = "paused" | "canceled"
    """

    def __init__(self, kind: str, run_id: str):
        super().__init__(f"run {run_id} {kind}")
        self.kind = kind
        self.run_id = run_id


class RunError(RuntimeError):
    """run 操作级错误（run 不存在/状态不允许/未知阶段等）。"""


def check_stop(ctrl: RunControl, stage: str, log: EventLog | None = None) -> None:
    """检查点：若 stop 已请求，抛 RunHalted。节点在单位边界调用。"""
    if ctrl.stop_requested:
        if log is not None:
            log.event(stage, "status", what="pause-requested")
        raise RunHalted("canceled" if ctrl.canceled else "paused", ctrl.run_id)


def run_in_thread(ctrl: RunControl, target: Callable[[], None],
                  name: str | None = None) -> threading.Thread:
    """在独立线程跑驱动（CLI 阻塞等待/Web 后台都复用这里）。"""
    t = threading.Thread(target=target, daemon=True,
                         name=name or f"cut-run-{ctrl.run_id}")
    t.start()
    return t


def pid_alive(pid: int | None) -> bool:
    """pid 是否存活（跨平台）。None=未知，按存活处理（保守：不误杀正在跑的旧 run）。"""
    if not pid:
        return True
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, ValueError):
        return True  # 无权限发信号 = 进程存在
    except Exception:
        return True
