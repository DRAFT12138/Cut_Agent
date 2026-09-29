# Agent decisions contract

Use UTF-8 JSON with `schema_version: 1`.

```json
{
  "schema_version": 1,
  "segments": [
    {
      "text": "Exact narration text",
      "duration": 4.2,
      "mood": "calm opening",
      "kw_cn": ["清晨", "城市"],
      "kw_en": ["city dawn"]
    }
  ],
  "media_descriptions": {
    "clip.mp4": "Wide shot of a quiet city street at dawn."
  },
  "timeline": [
    {
      "media": "clip.mp4",
      "segment_text": "Exact narration text",
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

- `segments` and `timeline` are required non-empty arrays.
- `segment_text` must equal a segment's `text`.
- `media` must be an exact filename from the exported context. Use `""` only for a web-needed row.
- `use_duration` must be positive. Keep `start_offset + use_duration` within the source duration for video.
- `media_descriptions` and `music` are optional. Descriptions are keyed by exact filename.
- Unknown top-level fields and unsupported schema versions are rejected before the run starts.

