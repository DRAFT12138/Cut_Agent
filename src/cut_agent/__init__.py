"""cut_agent：视频粗剪 agent。

输入：素材文件夹（视频/图片）+ 文案
输出：粗剪文档（时间线/镜头对齐/配乐/网络素材）
联网探索由 open-webSearch 守护进程（或 Playwright 回退）完成。

阶段驱动见 runs.py（run 目录 + 阶段产物 + 暂停/恢复/断电恢复）；
CLI 入口见 cli.py，Web 控制台见 server.py（Phase 0）。
"""
from .graph import run
from .runs import list_runs, resume_run, start_run, status_of

__all__ = ["run", "start_run", "resume_run", "status_of", "list_runs"]
