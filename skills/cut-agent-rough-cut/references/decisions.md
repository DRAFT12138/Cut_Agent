# Agent decisions contract

Use UTF-8 JSON with `schema_version: 3`; it is the only accepted version. Read IDs and observed frame times from `agent-context.json`; do not invent them. Version 3 deliberately allows several timeline shots to cover one narration segment, while requiring an auditable trajectory.

```json
{
  "schema_version": 3,
  "strategy": {
    "intent": "Open quietly, then increase visual tempo.",
    "pacing": "Mostly 3–4 second shots; use a two-shot montage for the turn."
  },
  "segments": [
    {
      "segment_id": "segment_001",
      "text": "Exact narration text",
      "duration": 4.2,
      "mood": "calm opening",
      "kw_cn": ["清晨", "城市"],
      "kw_en": ["city dawn"]
    }
  ],
  "media_descriptions": {
    "media_ffabf9ce7c33": "Wide shot of a quiet city street at dawn."
  },
  "timeline": [
    {
      "segment_id": "segment_001",
      "media_id": "media_ffabf9ce7c33",
      "use_duration": 4.2,
      "start_offset": 1.5,
      "needs_web": false
    }
  ],
  "trajectory": [
    {
      "step": 1,
      "action": "inspect_frames",
      "observation": "The 1.5s sample is a stable wide shot with an empty street.",
      "decision": "Use it as the restrained establishing shot.",
      "evidence": [{"media_id": "media_ffabf9ce7c33", "time": 1.5}]
    },
    {
      "step": 2,
      "action": "coverage_check",
      "observation": "Every segment is covered and all source ranges are in bounds.",
      "decision": "Accept the proposed timeline."
    }
  ],
  "music": {
    "mood": "calm cinematic",
    "primary": {
      "title": "Suggested style or track",
      "artist": "",
      "reason": "Supports a restrained opening."
    },
    "alternatives": []
  }
}
```

## Constraints

- `segments` and `timeline` are required non-empty arrays. Segment texts, concatenated in order and ignoring whitespace, must exactly equal the original copy.
- Give every segment a unique `segment_id`; the timeline must reference every ID at least once. Multiple consecutive rows may reference the same ID for a purposeful montage or cutaway.
- `media_id` must be copied from the exported context. Use `""` only with `needs_web: true`.
- `use_duration` must be positive. For video, `start_offset + use_duration` must not exceed `max_use_duration`; use `thumbnail_samples[].time` as visual evidence. Image offsets normalize to zero.
- `media_descriptions` and `music` are optional. Descriptions are keyed by `media_id`.
- `trajectory` is a required non-empty array. Every step needs a concise `action` and `decision`; add `observation` and source/time `evidence` whenever available. Do not put hidden chain-of-thought in it—record reviewable facts and decision summaries only.
- `strategy` is optional and can state intent, pacing, selection policy, and self-imposed limits.
- Unknown top-level fields and any schema version other than 3 are rejected before the run starts; there is no legacy coercion or fallback.
