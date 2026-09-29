# Cut Agent user guide

[中文使用说明](usage.zh-CN.md) · [README](../README.md)

## 1. Install

Install Python 3.10–3.14 and `uv` (or use `pip`) on Linux or Windows, plus FFmpeg 5 or newer with FFprobe. Confirm `ffmpeg -version` and `ffprobe -version` work in your terminal. The Python distribution includes the web frontend:

```bash
uv sync
```

`uv sync` uses the repository's lockfile. If you use pip, run `python -m pip install .` and replace `uv run cut-agent` below with `python -m cut_agent.cli`. Node.js with Corepack is required only to develop the frontend: start the backend on port 8090 and run `corepack pnpm dev` in `web/`; Vite serves port 5173 and proxies `/api` to the backend.

## 2. Configure the model

Cut Agent sends chat completion requests to an OpenAI-compatible endpoint. Its defaults are a **local example**, so set your endpoint and model before launching it. For PowerShell:

```powershell
$env:CUT_AGENT_LLM_BASE_URL = "http://127.0.0.1:8088/v1"
$env:CUT_AGENT_LLM_MODEL = "your-model-id"
$env:CUT_AGENT_LLM_API_KEY = "your-key-or-local-placeholder"
```

For a POSIX shell:

```bash
export CUT_AGENT_LLM_BASE_URL="http://127.0.0.1:8088/v1"
export CUT_AGENT_LLM_MODEL="your-model-id"
export CUT_AGENT_LLM_API_KEY="your-key-or-local-placeholder"
```

An unavailable model causes rule-based fallbacks and degraded-stage warnings. The client may retry slow requests, so a reachable endpoint makes the first run much faster. Image-capable chat models add shot descriptions; geometry-based shot detection remains available if image inference fails. Set `CUT_AGENT_VISION=0` to skip that step. A hosted model endpoint may receive your script and sampled frames: check your provider settings before using private footage.

## 3. Prepare media and start a task

Place videos and still images in one local folder and write a UTF-8 text file containing the script. To generate synthetic sample assets with FFmpeg:

```bash
uv run python make_sample.py
uv run cut-agent run --media sample_media --copy sample_copy.txt --seed 7
```

The CLI follows the task until it finishes. Press Ctrl+C to request a checkpointed pause; then run `uv run cut-agent resume RUN_ID`. The `--sync` mode blocks directly and does not use that cooperative Ctrl+C path. Add `--preview` to encode a numbered rough-cut MP4. Select delivery advice with `--platform douyin`, `xiaohongshu`, or `bilibili`. The platform option provides recommendations, not automatic publication.

For the web workspace, start `uv run cut-agent serve --port 8090` and open `http://127.0.0.1:8090`. Click **新建粗剪任务** (New rough-cut task), supply the media folder's **absolute path on the server machine**, paste the script, and optionally enable preview generation. The current interface is in Chinese. Keep the backend running while web tasks execute; after an unexpected shutdown, restart the server and resume interrupted tasks.

## 4. Review and edit

The task page groups eight stages into four phases:

1. **Prepare:** scan media, split the script, and understand shots.
2. **Assemble:** build the timeline, inspect supplemental web assets, and pick music.
3. **Review:** read the rough-cut document and, if requested, play the numbered preview.
4. **Handoff:** check the finishing guide and export the handoff package.

In the timeline stage, inspect the storyboard, move or replace shots, adjust duration, and review the critique. You can create another version from a completed task and compare the two in the A/B view. The active stage and timeline pane are encoded in the task URL for revisiting on the same machine. The handoff ZIP is the portable artifact; a task URL requires the local service and run directory.

CLI equivalents:

```bash
uv run cut-agent list
uv run cut-agent status RUN_ID
uv run cut-agent variant RUN_ID --seed 8
uv run cut-agent edit --run RUN_ID --move 1 2
uv run cut-agent edit --run RUN_ID --duration 1 3.5
uv run cut-agent rebuild --run RUN_ID --preview
```

`edit` line numbers are 1-based. Other operations are `--drop ROW`, `--swap ROW MEDIA` (optional `--shot N`), and `--append MEDIA` (optional `--shot N`). `rebuild` uses the saved `plan.json` without regenerating the complete pipeline. A fixed seed controls the request value, but model and external service changes can still change results.

## 5. Observability and recovery

Each run's `run.json` records its current attempt, recovery count, active stage, latest committed checkpoint, and bounded recovery history. `log.jsonl` is a durable structured event stream with the run ID, process ID, monotonic sequence, and timestamp. A `checkpoint` event is appended after every stage artifact is atomically committed, so observers can distinguish in-flight work from reusable progress. Run `cut-agent status <run-id>` to see the summary.

After an unexpected process exit, `status`, `list`, or server startup detects runs that no longer own a worker, marks them `interrupted`, and identifies `resumable_from`. Run `cut-agent resume <run-id>` to validate the media snapshot, reuse every contiguous completed stage, increment the attempt, and record the resume point and reused stages in both the event stream and `recovery_history`. Recovery is rejected if source media changed, preventing stale checkpoints from being applied to new input.

## 6. Outputs and handoff

Each task writes to `output/runs/RUN_ID/`. Key files include:

| File | Use |
| --- | --- |
| `run.json`, `log.jsonl`, `stages/*.json` | Status, event history, and resumable stage artifacts. |
| `plan.json`, `plan.md` | Editable timeline source and rendered plan. |
| `粗剪方案.md`, `cut_lines.txt` | Rough-cut document and cut-by-cut instructions. |
| `精剪指导.md`, `frames/` | Finishing advice and representative frames. |
| `preview.mp4` | Optional preview, created only when requested. |

The **精剪交接包.zip** download includes an offline HTML reader, current documents, review checks, frames, and the preview if generated. Extract it and open `精剪交接.html` in a browser. Its review checks are read-only; update checks in the web workspace and download the package again. Bring the original footage, any selected web assets, and music source files separately into your editing software. Verify externally sourced media and licensing before publishing a video.

Generated media, browser profiles, caches, and run outputs are ignored by Git. Do not commit private scripts or footage.

## Optional web search

The bundled `tools/open-webSearch` is an independent Apache-2.0 project. Cut Agent prefers its local daemon at `CUT_AGENT_OWS_BASE_URL` and can fall back to browser search. Its absence should not stop local planning; unresolved supplemental footage is marked for manual replacement.

```bash
cd tools/open-webSearch
npm ci
npm run build
node build/index.js serve
```

On Windows, `pwsh tools/start_owsearch.ps1 -NoProxy` is a convenience launcher from the repository root; `-ProxyUrl URL` can set a proxy. Set `CUT_AGENT_CHROME` if Chrome is installed outside the default Windows path. External search results and music availability can change over time.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `ffmpeg` / `ffprobe` missing | Install both programs and reopen the terminal so `PATH` is updated. |
| Web page reports missing packaged assets | Reinstall an official wheel or rebuild with `uv run python tools/build_web_assets.py`. |
| New task rejects the media path | Use an existing absolute directory path visible to the backend, not the browser machine's path. |
| Model stage is slow or degraded | Check `CUT_AGENT_LLM_BASE_URL`, model ID, key, endpoint availability, and image support. |
| No preview MP4 | Enable preview when creating the task or rebuild with `--preview`. |
| Run is interrupted | Restart the local service and use Resume in the UI or `cut-agent resume RUN_ID`. |
