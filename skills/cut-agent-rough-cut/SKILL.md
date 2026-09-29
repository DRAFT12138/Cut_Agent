---
name: cut-agent-rough-cut
description: Inspect a local video/image folder, design media-aware narration segments and a timeline, and run Cut Agent without an OpenAI-compatible model API. Use when Codex, Claude Code, or another coding agent is asked to create, revise, compare, or export a rough cut from local media and narration.
---

# Cut Agent rough cut

Use Cut Agent's file-based agent interface. Do not invent media names or claim to have inspected frames you did not open.

## Create a rough cut

1. Export deterministic media context:

   ```bash
   uv run cut-agent agent-context --media /absolute/media/path --copy script.txt --output agent-context.json
   ```

2. Read `agent-context.json`. Inspect every image listed in `thumbnail_samples`; use each sample's `time` when choosing a video offset. Use the available image-viewing tool—filenames and metadata alone are insufficient for visual matching.
3. Work as an evidence-driven editing loop rather than filling a fixed template:
   - observe representative frames and source timing;
   - form a pacing/visual strategy;
   - propose one or more shots for each narration segment;
   - check coverage, source bounds, repetition, and rhythm;
   - revise when a check fails.
4. Write `agent-decisions.json` following [references/decisions.md](references/decisions.md). Preserve and completely cover the narration, reference `segment_id` and `media_id`, and record material observations and choices in `trajectory`. Multiple shots may cover one segment when the pacing benefits; avoid cuts that have no editorial purpose.
5. Run without a model endpoint:

   ```bash
   uv run cut-agent run \
     --media /absolute/media/path --copy script.txt \
     --agent-decisions agent-decisions.json --no-finishing-llm --preview
   ```

6. Report the run ID. Inspect `output/runs/<run-id>/plan.json`, the preview, critique warnings, and `agent/trajectory.json`. The same steps also appear in `log.jsonl` as `agent` events. If the result needs adjustment, prefer `cut-agent edit`; regenerate the decisions only for structural changes.

## Revise safely

- Treat `agent-context.json` as source evidence and `agent-decisions.json` as the agent's authored proposal.
- Never modify source media.
- Prefer local media. Use an empty `media` plus `needs_web: true` only when no local shot fits.
- Use the fewest shots that express the intended rhythm; one row per segment is a baseline, not a restriction.
- State clearly when visual inspection is unavailable.
- Do not add API keys or private scripts to the repository.
