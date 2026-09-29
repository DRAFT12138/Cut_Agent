"""cut_agent 配置。所有路径与本地服务参数集中在此。"""
from __future__ import annotations

import os
from pathlib import Path

# 项目根目录（本文件位于 <root>/src/cut_agent/config.py）
ROOT = Path(__file__).resolve().parent.parent.parent

# 本地 LLM（llama.cpp / OpenAI 兼容端点）
LLM_BASE_URL = os.environ.get("CUT_AGENT_LLM_BASE_URL", "http://127.0.0.1:8088/v1")
LLM_API_KEY = os.environ.get("CUT_AGENT_LLM_API_KEY", "sk-no-key-required")
LLM_MODEL = os.environ.get("CUT_AGENT_LLM_MODEL", "Qwen3.8-27B-UD-Q4_K_XL.gguf")

# 真实 Chrome 可执行文件
CHROME_PATH = os.environ.get(
    "CUT_AGENT_CHROME",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

# open-webSearch 本地守护进程（多引擎搜索 + 网页抓取）
# 启动：cd tools/open-webSearch && node build/index.js serve
OWS_BASE_URL = os.environ.get("CUT_AGENT_OWS_BASE_URL", "http://127.0.0.1:3210")
OWS_SRC_DIR = ROOT / "tools" / "open-webSearch"

# 免版权 BGM 源（freepd.com 已于 2026-01 关站，用镜像 freepd.cn 的 CC0 直链）
FREEPD_BASE = "https://freepd.cn"

# Playwright 用户数据目录（复用 Chrome 配置，便于保留登录态/字体）
PROFILE_DIR = ROOT / ".chrome-profile"

# 素材 / 输出
SAMPLE_MEDIA = ROOT / "sample_media"
WORK_DIR = ROOT / "work"          # 下载的配乐、缩略图、日志
OUTPUT_DIR = ROOT / "output"      # 最终粗剪文档

for p in (WORK_DIR, OUTPUT_DIR, SAMPLE_MEDIA):
    p.mkdir(parents=True, exist_ok=True)

# 浏览器抓取超时
FETCH_TIMEOUT_MS = 25_000
SEARCH_TIMEOUT_MS = 30_000

# 视觉层（变化驱动自适应抽帧 + 多模态 LLM 镜头描述）
# 关闭时 understand_media 跳过，build_timeline 退回"文件名+元数据"匹配
VISION_ENABLED = os.environ.get("CUT_AGENT_VISION", "1") not in ("0", "false", "False", "")
