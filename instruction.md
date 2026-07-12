# IAM firewall drift incident containment

Act as the security operations engineer recovering an IAM firewall-policy control after a failed enforcement rollout. Restore `/app/workflow/export_report.py`, the control-plane reconciler that converts firewall drift alerts into responder containment work. Preserve its operational CLI: `--input PATH` and `--output-dir PATH`, defaulting to `/app/data/events.json` and `/app/output`.

This is security-control reconciliation, not application feature development. The resulting artifacts decide which IAM firewall drifts require immediate containment, which are attenuated by approved policy windows, and how risk carries between repeated drifts in the same environment.

Always read freezes and scoped attenuation policies from their fixed absolute paths:
`/app/data/change_freezes.json`, `/app/data/reopen_windows.json`, `/app/data/rotation_windows.json`, and `/app/data/defer_windows.json`. `--input` changes only the event stream; policy files never become relative to input/output directories.

`/app/docs/report_spec.json` is the authoritative security runbook for normalization, deterministic alert deduplication, policy-window compaction, scoped attenuation, risk-ledger state progression, containment queue ranking, evidence schemas, and integrity-checksum serialization. Follow its merge and tie-break rules exactly.

Scoped attenuation uses severity scope allowlists. During reopen/rotation/defer canonicalization, keep rows only when `severity_scope` is in the source `scope_values`; drop others before compaction/checksum computation. Matching scopes are `{all, max_severity}` for each window, with one fallback: for `p2` windows, if that source has no `(env,p2)` compacted intervals, borrow `(env,p1)` as the severity scope.

The risk ledger is sequential inside each normalized environment. A finalized drift window’s carry state decays across the idle gap and feeds the next window; repeated drift in one environment cannot be assessed independently.

For summary generation, apply byte-level checksum serialization exactly as specified (UTF-8, pipe/LF delimiters, no trailing newline, normalized `muted` encoded as `0/1`). Probe overlaps count `all` and severity scopes independently. Max pressure/stability fields are queue-only, while `max_carry_out_ms` spans all drift windows.

Write exactly `summary.json`, `drift_windows.json`, and compact `response_queue.jsonl` under the requested output directory. These are security evidence and responder-queue artifacts: they must generalize to alternate alert streams, remain idempotent across reruns, and derive only from operational sources. Keep the frozen incident snapshot `/app/workflow/.export_report.original` unchanged and never read/import verifier artifacts.