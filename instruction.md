Repair `/app/workflow/export_report.py`. It must compile IAM firewall drift alerts into merged chain windows and a deterministic response queue.

Keep CLI behavior:
- `python3 /app/workflow/export_report.py --input PATH --output-dir PATH`
- default input: `/app/data/events.json`
- default output dir: `/app/output`
- freeze windows are always loaded from `/app/data/change_freezes.json`
- reopen windows are always loaded from `/app/data/reopen_windows.json`

The repaired workflow must write exactly three files:
- `summary.json`
- `drift_windows.json`
- `response_queue.jsonl`

Core processing requirements:
1. Canonicalize alerts:
   - normalize `env` and `severity` with `str(...).strip().lower()`
   - normalize `signature` by collapsing internal whitespace
   - coerce `end_ms` to int (`str(...).strip()` before conversion; invalid => `0`)
   - muted coercion:
     - booleans unchanged
     - strings: only `"true"`, `"1"`, `"yes"` => `True`; all other strings => `False`
     - non-string/non-bool values use Python `bool(value)`
   - dedupe by `alert_id`: keep largest `end_ms`; tie-break by severity rank `p1 > p2 > p3 > p4`, then longer normalized signature, then lexicographically larger normalized env; if still tied after all listed rules, keep the first seen row in input order
2. Build merged windows from unmuted alerts only, grouped by normalized env.
   - merge condition: `next.start_ms <= current.end_ms + 45`
   - per window compute base fields:
     - `start_ms`, `end_ms`, `duration_ms`
     - `alert_count`
     - `source_alert_ids` sorted ascending
     - `max_severity`
3. Apply same-env freeze overlap using `/app/data/change_freezes.json`:
   - canonicalize freeze rows before overlap:
     - normalize freeze `env` with the same env normalization as alerts
     - normalize freeze `start_ms` / `end_ms` with the same int coercion
     - ignore freeze rows where `end_ms <= start_ms`
     - compact freezes per env by merging overlapping or touching intervals (`next.start_ms <= current.end_ms`)
   - overlap formula: `max(0, min(end_a, end_b) - max(start_a, start_b))`
   - `freeze_overlap_ms` is summed overlap against all matching freeze windows
   - `freeze_segment_count` is the number of distinct overlap segments contributing to `freeze_overlap_ms`
   - `effective_duration_ms = max(duration_ms - freeze_overlap_ms, 0)`
4. Apply same-env reopen attenuation using `/app/data/reopen_windows.json`:
   - canonicalize reopen rows:
     - normalize reopen `env` with the same env normalization
     - normalize `severity_scope` via `str(...).strip().lower()` with supported values: `all`, `p1`, `p2`
     - normalize reopen `start_ms` / `end_ms` with the same int coercion
     - ignore reopen rows where `end_ms <= start_ms`
     - compact reopen intervals per `(env, severity_scope)` by merging overlapping or touching intervals
   - for each merged window, consider matching reopen scopes `{all, max_severity}` for the same env
   - compute overlap segments from both scopes, compact those overlap segments (union), then:
     - `reopen_overlap_ms`: total union overlap against the window
     - `reopen_segment_count`: compacted overlap segment count
   - `risk_adjusted_duration_ms = max(effective_duration_ms - (reopen_overlap_ms // 2), 0)`

Queue rules (`response_queue.jsonl`):
- include only windows where:
  - `max_severity` is `p1` or `p2`
  - severity-specific risk-adjusted minimum passes:
    - `p1`: `risk_adjusted_duration_ms >= 200`
    - `p2`: `risk_adjusted_duration_ms >= 260`
- `ticket_id` format: `"{env}:{start_ms}-{end_ms}"`
- compute `stability_pressure_score` per queued row from probe window `[end_ms-180, end_ms+1)`:
  - `all_probe_ms`: overlap against compacted reopen windows `(env, all)`
  - `severity_probe_ms`: overlap against compacted reopen windows `(env, max_severity)`
  - `stability_pressure_score = (all_probe_ms // 30) + (severity_probe_ms // 20) + max(alert_count - 1, 0)`
- `queue_hash`: first 12 lowercase hex chars of SHA1 over `"{env}|{start_ms}|{end_ms}|{','.join(source_alert_ids)}|{max_severity}|{freeze_segment_count}|{reopen_segment_count}|{stability_pressure_score}"`
- `window_digest`: first 10 lowercase hex chars of SHA1 over `"{ticket_id}|{priority}|{risk_adjusted_duration_ms}|{stability_pressure_score}"`
- priority:
  - `critical` if (`max_severity == "p1"` and `risk_adjusted_duration_ms >= 260`) or `risk_adjusted_duration_ms >= 560` or `stability_pressure_score >= 14`
  - `high` if `risk_adjusted_duration_ms >= 300` or (`alert_count >= 3` and `max_severity` in `{"p1", "p2"}`) or (`reopen_segment_count == 0` and `duration_ms >= 420`)
  - else `medium`
- final sort order:
  1. priority rank (`critical > high > medium`)
  2. `risk_adjusted_duration_ms` descending
  3. `stability_pressure_score` descending
  4. `freeze_segment_count` descending
  5. `alert_count` descending
  6. `env` ascending
  7. `start_ms` ascending
- JSONL rows must be compact (`json.dumps(row, separators=(",", ":"))`)

Output schema requirements:
- `drift_windows.json`: flat map `{env: [window, ...]}`, env keys sorted ascending, windows sorted by `start_ms`; each window has exactly:
  `start_ms`, `end_ms`, `duration_ms`, `freeze_overlap_ms`, `freeze_segment_count`, `effective_duration_ms`, `reopen_overlap_ms`, `reopen_segment_count`, `risk_adjusted_duration_ms`, `alert_count`, `source_alert_ids`, `max_severity`
- `response_queue.jsonl` rows must have exactly:
  `ticket_id`, `env`, `start_ms`, `end_ms`, `duration_ms`, `freeze_overlap_ms`, `freeze_segment_count`, `effective_duration_ms`, `reopen_overlap_ms`, `reopen_segment_count`, `risk_adjusted_duration_ms`, `alert_count`, `source_alert_ids`, `max_severity`, `stability_pressure_score`, `priority`, `queue_hash`, `window_digest`
- `summary.json` must have exactly:
  `schema_version`, `raw_alert_count`, `unique_alert_ids`, `canonical_alert_count`, `env_count`, `severity_counts`, `total_unmuted_duration_ms`, `total_freeze_overlap_ms`, `total_freeze_segment_count`, `total_effective_duration_ms`, `total_reopen_overlap_ms`, `total_reopen_segment_count`, `total_risk_adjusted_duration_ms`, `longest_window_ms`, `queued_window_count`, `muted_excluded_count`, `max_stability_pressure_score`, `canonical_alert_checksum`, `queue_hash_checksum`, `freeze_compaction_checksum`, `reopen_compaction_checksum`, `window_digest_checksum`
- summary specifics:
  - `schema_version` must be `"firewall-drift-v1"`
  - `severity_counts` key order: `p1`, `p2`, `p3`, `p4`
- `canonical_alert_checksum`: SHA256 over canonical alert rows in canonical order (`env`, `start_ms`, `alert_id`) with line format `alert_id|env|start_ms|end_ms|severity|muted_int|signature`, where `muted_int` is `1` or `0`
- `queue_hash_checksum`: SHA256 of `"|".join(queue_hash values in final queue order)` and must be 64 lowercase hex chars
- `freeze_compaction_checksum`: SHA256 over compacted freeze rows in canonical order (`env`, `start_ms`, `end_ms`) with line format `env|start_ms|end_ms`
- `reopen_compaction_checksum`: SHA256 over compacted reopen rows in canonical order (`env`, `severity_scope`, `start_ms`, `end_ms`) with line format `env|severity_scope|start_ms|end_ms`
- `window_digest_checksum`: SHA256 of `"|".join(window_digest values in final queue order)` and must be 64 lowercase hex chars

Keep `/app/workflow/.export_report.original` unchanged. Do not read/import verifier artifacts from `/tests`.
