# Cut Agent

[简体中文](README.zh-CN.md) · [User guide](docs/usage.md) · [中文使用说明](docs/usage.zh-CN.md)

Cut Agent turns a folder of video or image clips and a script into an **editable rough-cut plan**. Its local web workspace helps you review shots, adjust the timeline, compare two versions, generate an optional numbered preview, and hand a finishing package to an editor.

![Cut Agent workflow: scan media and script, assemble a rough cut, review it, then hand off finishing instructions](docs/assets/workflow.svg)

> Cut Agent plans the edit. It does not produce a finished, color-graded, subtitled film. The optional preview is for checking timing and structure.

## What it does

- Scans local video and image assets, detects shots, and extracts representative frames.
- Splits the script, proposes a timeline, and records editable `plan.json` plus a readable rough-cut document.
- Searches for supplemental footage and music when their sources are available; records gaps for manual review when they are not.
- Supports timeline edits, A/B variants, pause/resume, and checkpoint recovery in a local web workspace.
- Lets the LLM plan beat-aware animated scenes for continuous or transition-only music, author an offline HTML/CSS/JavaScript page for each, and insert the frame-rendered transition without an image/video generation model.
- Exports a finishing guide and an offline-readable handoff ZIP. Source footage and downloaded media must be supplied separately to the editor.
- Lets Codex, Claude Code, and other coding agents inspect media context and author rough-cut decisions without a separate model API.

## Quick start

**Requirements:** Linux or Windows, Python 3.10–3.14, and FFmpeg **and** FFprobe 5+ on `PATH`. Released wheels include the web UI, so Node.js is needed only for frontend development. An OpenAI-compatible chat endpoint is recommended; without one, planning uses rule-based fallbacks and records degraded stages. Chrome and the optional search daemon improve web asset discovery.

```bash
git clone https://github.com/DRAFT12138/Cut_Agent.git
cd Cut_Agent
uv sync
uv run python make_sample.py
uv run cut-agent serve --port 8090
```

Open **http://127.0.0.1:8090**, choose **New rough-cut task**, enter the absolute path to `sample_media`, and paste the text in `sample_copy.txt`. Keep the terminal running while a web task runs. For a CLI-only example:

```bash
uv run cut-agent run --media sample_media --copy sample_copy.txt --seed 7
uv run cut-agent list
```

If you use another Python environment, `python -m pip install .` and `python -m cut_agent.cli ...` provide the same CLI. The Python package includes a prebuilt UI, but does not ship a model, FFmpeg, or generated media. Frontend contributors can refresh that embedded UI with `uv run python tools/build_web_assets.py`.

### Use a coding agent instead of a model API

Export deterministic context, ask the agent to inspect the listed thumbnails and write `agent-decisions.json`, then run the normal pipeline with those decisions:

```bash
uv run cut-agent agent-context --media sample_media --copy sample_copy.txt --output agent-context.json
uv run cut-agent run --media sample_media --copy sample_copy.txt \
  --agent-decisions agent-decisions.json --no-finishing-llm --preview
```

See the repository's [`cut-agent-rough-cut` skill](skills/cut-agent-rough-cut/SKILL.md) for the workflow and decision contract.
Copy that directory into `${CODEX_HOME:-~/.codex}/skills/` for Codex, or load it with another coding agent that supports `SKILL.md`. The CLI file protocol also works without installing the skill.

## Model and optional services

Set these environment variables **before** starting Cut Agent. Defaults target a local OpenAI-compatible service at `http://127.0.0.1:8088/v1`.

| Variable | Purpose |
| --- | --- |
| `CUT_AGENT_LLM_BASE_URL` | Chat completions base URL, including `/v1`. |
| `CUT_AGENT_LLM_MODEL` | Model ID accepted by that endpoint. |
| `CUT_AGENT_LLM_API_KEY` | API key; default is a local placeholder. |
| `CUT_AGENT_VISION=0` | Skip multimodal video descriptions. |
| `CUT_AGENT_CHROME` | Chrome executable path for browser-backed search. |
| `CUT_AGENT_OWS_BASE_URL` | Optional open-webSearch daemon URL; default `http://127.0.0.1:3210`. |

For best shot descriptions, the model endpoint must accept image inputs. Search and music depend on external sites and can degrade; inspect their results and usage rights before delivery. See the [English guide](docs/usage.md) or [中文使用说明](docs/usage.zh-CN.md) for setup, workflow, CLI commands, outputs, and troubleshooting.

## Development

```bash
uv run pytest -q -m 'not media'
uv run pytest -q -m media
uv run ruff check src tests
cd web
corepack pnpm test
corepack pnpm build
cd ..
uv run python tools/verify_ui.py smoke
uv build && uv run python tools/check_release.py
```

See the [release checklist](docs/RELEASING.md) for isolated Linux/Windows installation checks and publishing steps.

Contributions are welcome through issues and pull requests. Please include steps to reproduce bugs and run the relevant checks. The Python app is licensed under [MIT](LICENSE). The bundled `tools/open-webSearch` source is a separate [Apache-2.0 project](tools/open-webSearch/LICENSE); see [third-party notices](THIRD_PARTY_NOTICES.md).
