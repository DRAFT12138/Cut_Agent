# Agent decisions contract

Use UTF-8 JSON with `schema_version: 2`. Read IDs and observed frame times from `agent-context.json`; do not invent them.

```json
{
  "schema_version": 2,
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
- Give every segment a unique `segment_id`; the first-pass timeline must reference every ID exactly once.
- `media_id` must be copied from the exported context. Use `""` only with `needs_web: true`.
- `use_duration` must be positive. For video, `start_offset + use_duration` must not exceed `max_use_duration`; use `thumbnail_samples[].time` as visual evidence. Image offsets normalize to zero.
- `media_descriptions` and `music` are optional. Descriptions are keyed by `media_id`.
- Unknown top-level fields and unsupported schema versions are rejected before the run starts.
