# Cut Agent 使用说明

[English user guide](usage.md) · [返回 README](../README.zh-CN.md)

## 1. 安装

在 Linux 或 Windows 上准备 Python 3.10–3.14、`uv`（或 `pip`），以及 FFmpeg 与 FFprobe 5+。在终端确认 `ffmpeg -version` 和 `ffprobe -version` 都能运行。Python 发行物已包含 Web 前端；只有前端开发需要带 Corepack 的 Node.js：

```bash
uv sync
cd web
corepack pnpm install --frozen-lockfile
corepack pnpm build
cd ..
```

`uv sync` 使用仓库中的锁文件。使用 pip 时，运行 `python -m pip install -e .`，并将下文的 `uv run cut-agent` 替换为 `python -m cut_agent.cli`。开发前端时，先启动 8090 端口的后端，再在 `web/` 运行 `corepack pnpm dev`；Vite 使用 5173 端口并把 `/api` 转发到后端。

## 2. 配置模型

Cut Agent 请求 OpenAI 兼容的聊天接口。仓库默认地址只是**本机示例**，建议在启动前配置实际接口与模型。PowerShell 示例：

```powershell
$env:CUT_AGENT_LLM_BASE_URL = "http://127.0.0.1:8088/v1"
$env:CUT_AGENT_LLM_MODEL = "your-model-id"
$env:CUT_AGENT_LLM_API_KEY = "your-key-or-local-placeholder"
```

Linux/macOS shell 示例：

```bash
export CUT_AGENT_LLM_BASE_URL="http://127.0.0.1:8088/v1"
export CUT_AGENT_LLM_MODEL="your-model-id"
export CUT_AGENT_LLM_API_KEY="your-key-or-local-placeholder"
```

模型不可用时会回退到规则方案，并显示阶段降级原因。客户端可能重试较慢的请求，因此首次体验建议使用可连接的模型接口。支持图片输入的模型可补充镜头描述；图片推理失败仍可保留几何镜头识别。设置 `CUT_AGENT_VISION=0` 可跳过多模态理解。使用云端模型时，文案及抽样帧可能发送给该服务；处理私有素材前请确认服务配置。

## 3. 准备素材并启动

把视频与图片放在同一个本地文件夹，另建 UTF-8 编码文案文件。可用 FFmpeg 生成不含私人内容的示例素材：

```bash
uv run python make_sample.py
uv run cut-agent run --media sample_media --copy sample_copy.txt --seed 7
```

CLI 默认持续跟随任务直至结束。按 Ctrl+C 会请求在检查点暂停，之后运行 `uv run cut-agent resume RUN_ID`；`--sync` 为直接阻塞模式，不走这一协作暂停流程。加 `--preview` 才会生成带段号的粗剪预览 MP4。`--platform` 可选 `douyin`、`xiaohongshu`、`bilibili`，用于交付建议，不会自动发布平台内容。

Web 方式：运行 `uv run cut-agent serve --port 8090`，访问 `http://127.0.0.1:8090`，点击「新建粗剪任务」。素材目录必须填**服务所在机器可访问的绝对路径**；粘贴文案并按需勾选预览。Web 任务运行时保持服务开启；意外退出后重新启动服务，并恢复标记为中断的任务。

## 4. 核对与调整

任务页把八个阶段分为四组：

1. **准备素材与脚本：** 扫描素材、文案分段、理解镜头。
2. **搭建粗剪：** 建立时间线、核对网络素材、选择配乐。
3. **检查粗剪：** 阅读粗剪文档，并按需播放带段号的预览。
4. **交接精剪：** 核对精剪指导，导出交接包。

在时间线阶段可看分镜、移动或替换镜头、修改时长并核对自检。完成任务后可生成另一版，再进入 A/B 页并排比较。任务 URL 记录当前阶段和时间线子页面，适合同一台机器回访；跨机器交接请下载 ZIP，因为 URL 依赖本地服务和任务目录。

对应 CLI 命令：

```bash
uv run cut-agent list
uv run cut-agent status RUN_ID
uv run cut-agent variant RUN_ID --seed 8
uv run cut-agent edit --run RUN_ID --move 1 2
uv run cut-agent edit --run RUN_ID --duration 1 3.5
uv run cut-agent rebuild --run RUN_ID --preview
```

`edit` 行号从 1 开始。还可用 `--drop ROW`、`--swap ROW MEDIA`（可附 `--shot N`）、`--append MEDIA`（可附 `--shot N`）。`rebuild` 依据已有 `plan.json` 重建导出，不重跑完整流水线。固定 seed 只固定请求参数；模型和外部服务变化仍可能改变结果。

## 5. 产物与交接

每个任务写入 `output/runs/RUN_ID/`，主要文件如下：

| 文件 | 用途 |
| --- | --- |
| `run.json`、`log.jsonl`、`stages/*.json` | 状态、事件记录、可恢复的阶段产物。 |
| `plan.json`、`plan.md` | 可编辑的时间线事实源及其文档。 |
| `粗剪方案.md`、`cut_lines.txt` | 粗剪方案与逐刀指令。 |
| `精剪指导.md`、`frames/` | 精剪建议与代表帧。 |
| `preview.mp4` | 仅在明确请求时生成的预览。 |

「精剪交接包.zip」包含可离线打开的 HTML 阅读页、当前文档、核对记录、配图及已生成的预览。解压后用浏览器打开 `精剪交接.html`。离线页面中的核对框是只读的；需要回 Web 更新状态并重新下载交接包。原始素材、选用的网络素材和配乐源文件需单独带入剪辑软件。发布视频前请核对网络素材及音乐的使用权。

生成的媒体、浏览器配置、缓存和任务输出已被 Git 忽略。不要把私人文案或拍摄素材提交到仓库。

## 可选网络检索

仓库内的 `tools/open-webSearch` 是独立的 Apache-2.0 项目。Cut Agent 优先连接 `CUT_AGENT_OWS_BASE_URL` 指向的本地守护进程，也可回退到浏览器检索。即使没有它，本地粗剪规划仍可运行；缺失的补充画面会标为待人工替换。

```bash
cd tools/open-webSearch
npm ci
npm run build
node build/index.js serve
```

Windows 可从仓库根目录运行 `pwsh tools/start_owsearch.ps1 -NoProxy`；需要代理时传 `-ProxyUrl URL`。Chrome 不在默认 Windows 路径时，设置 `CUT_AGENT_CHROME`。外部搜索结果和音乐可用性可能随时间变化。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 找不到 `ffmpeg` / `ffprobe` | 安装两个程序，并重新打开终端使 `PATH` 生效。 |
| Web 页面只有占位提示 | 构建 `web/` 后重启 `cut-agent serve`。 |
| 新建任务提示素材路径无效 | 填后端可访问的绝对目录路径，而非另一台浏览器机器上的路径。 |
| 模型阶段缓慢或降级 | 检查模型地址、模型 ID、密钥、服务连通性及图片输入支持情况。 |
| 没有预览 MP4 | 新建任务时勾选预览，或运行 `rebuild --preview`。 |
| 任务显示中断 | 重启本地服务，在 Web 点「恢复」或运行 `cut-agent resume RUN_ID`。 |
