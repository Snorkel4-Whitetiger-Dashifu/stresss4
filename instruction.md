Repair `/app/workflow/export_report.py` so it compiles IAM firewall drift alerts into deterministic response artifacts. Keep the documented `--input` and `--output-dir` CLI, including defaults, and always load change-freeze, reopen, rotation, and defer data from their fixed paths under `/app/data`.

The authoritative behavioral contract is `/app/docs/report_spec.json`. Implement its normalization, deterministic deduplication, interval compaction, scoped attenuation, stateful cross-window risk ledger, queue classification, ordering, schemas, and checksums exactly. The ledger is sequential within each normalized environment: a completed window’s carry state decays across the idle gap and feeds the next window, so windows cannot be evaluated independently.

Write exactly `summary.json`, `drift_windows.json`, and compact `response_queue.jsonl` beneath the requested output directory. Outputs must generalize to alternate inputs, remain byte-logically stable across reruns, and use only source data—not verifier fixtures or hardcoded expected values.

Keep `/app/workflow/.export_report.original` unchanged and do not read or import anything from `/tests`.
