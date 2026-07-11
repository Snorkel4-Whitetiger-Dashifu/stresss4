Repair `/app/workflow/export_report.py`. It should compile IAM firewall drift alerts into merged chain windows and a deterministic response queue.

Keep CLI behavior:
- `python3 /app/workflow/export_report.py --input PATH --output-dir PATH`
- default input: `/app/data/events.json`
- default output dir: `/app/output`
- freeze windows are loaded from `/app/data/change_freezes.json`

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
   - per window compute:
     - `start_ms`, `end_ms`, `duration_ms`
     - `alert_count`
     - `source_alert_ids` sorted ascending
     - `max_severity`
3. Apply same-env freeze overlap using `/app/data/change_freezes.json`:
   - overlap formula: `max(0, min(end_a, end_b) - max(start_a, start_b))`
   - `freeze_overlap_ms` is summed overlap against all matching freeze windows
   - `effective_duration_ms = max(duration_ms - freeze_overlap_ms, 0)`

Queue rules (`response_queue.jsonl`):
- include only windows where:
  - `effective_duration_ms >= 180`
  - `max_severity` is `p1` or `p2`
- `ticket_id` format: `"{env}:{start_ms}-{end_ms}"`
- `queue_hash`: first 12 lowercase hex chars of SHA1 over `"{env}|{start_ms}|{end_ms}|{','.join(source_alert_ids)}"`
- priority:
  - `critical` if (`max_severity == "p1"` and `effective_duration_ms >= 280`) or `effective_duration_ms >= 600`
  - `high` if `effective_duration_ms >= 300` or (`alert_count >= 3` and `max_severity` in `{"p1", "p2"}`)
  - else `medium`
- final sort order:
  1. priority rank (`critical > high > medium`)
  2. `effective_duration_ms` descending
  3. `alert_count` descending
  4. `env` ascending
  5. `start_ms` ascending
- JSONL rows must be compact (`json.dumps(row, separators=(",", ":"))`)

Output schema requirements:
- `drift_windows.json`: flat map `{env: [window, ...]}`, env keys sorted ascending, windows sorted by `start_ms`; each window has exactly:
  `start_ms`, `end_ms`, `duration_ms`, `freeze_overlap_ms`, `effective_duration_ms`, `alert_count`, `source_alert_ids`, `max_severity`
- `response_queue.jsonl` rows must have exactly:
  `ticket_id`, `env`, `start_ms`, `end_ms`, `duration_ms`, `freeze_overlap_ms`, `effective_duration_ms`, `alert_count`, `source_alert_ids`, `max_severity`, `priority`, `queue_hash`
- `summary.json` must have exactly:
  `schema_version`, `raw_alert_count`, `unique_alert_ids`, `canonical_alert_count`, `env_count`, `severity_counts`, `total_unmuted_duration_ms`, `total_freeze_overlap_ms`, `total_effective_duration_ms`, `longest_window_ms`, `queued_window_count`, `muted_excluded_count`, `queue_hash_checksum`
- summary specifics:
  - `schema_version` must be `"firewall-drift-v1"`
  - `severity_counts` key order: `p1`, `p2`, `p3`, `p4`
  - `queue_hash_checksum` is SHA256 of `"|".join(queue_hash values in final queue order)` and must be 64 lowercase hex chars

Keep `/app/workflow/.export_report.original` unchanged. Do not read/import verifier artifacts from `/tests`.
