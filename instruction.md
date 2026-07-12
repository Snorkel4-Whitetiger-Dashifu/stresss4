Repair `/app/workflow/export_report.py`, the IAM firewall-drift compiler that emits responder work. Preserve the existing CLI: `--input PATH` and `--output-dir PATH` with defaults `/app/data/events.json` and `/app/output`.

Always read freezes and scoped attenuation policies from their fixed absolute paths:
`/app/data/change_freezes.json`, `/app/data/reopen_windows.json`, `/app/data/rotation_windows.json`, and `/app/data/defer_windows.json`. `--input` changes only the event stream; policy files never become relative to input/output directories.

`/app/docs/report_spec.json` is the authoritative contract for normalization, deterministic deduplication, interval compaction, scoped attenuation, risk ledger state progression, queue ranking, schemas, and checksum serialization. Follow the merge and tie-break rules exactly.

Scoped attenuation uses severity scope allowlists. During reopen/rotation/defer canonicalization, keep rows only when `severity_scope` is in the source `scope_values`; drop others before compaction/checksum computation. Matching scopes are `{all, max_severity}` for each window, with one fallback: for `p2` windows, if that source has no `(env,p2)` compacted intervals, borrow `(env,p1)` as the severity scope.

The risk ledger is sequential inside each normalized environment. A finalized window’s carry state decays across the idle gap and feeds the next window; windows in the same environment cannot be evaluated independently.

For summary generation, apply byte-level checksum serialization exactly as specified (UTF-8, pipe/LF delimiters, no trailing newline, normalized `muted` encoded as `0/1`). Probe overlaps count `all` and severity scopes independently. Max pressure/stability fields are queue-only, while `max_carry_out_ms` spans all drift windows.

Write exactly `summary.json`, `drift_windows.json`, and compact `response_queue.jsonl` under the requested output directory. Outputs must generalize to alternate inputs, be idempotent across reruns, and derive from source data only. Keep `/app/workflow/.export_report.original` unchanged and never read/import from `/tests`.