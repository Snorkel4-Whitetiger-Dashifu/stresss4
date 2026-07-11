#!/usr/bin/env python3
"""Reference fix for firewall drift chain compiler."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = "firewall-drift-v1"
FREEZE_PATH = Path("/app/data/change_freezes.json")
SEVERITY_ORDER = ["p1", "p2", "p3", "p4"]
SEVERITY_RANK = {name: len(SEVERITY_ORDER) - idx for idx, name in enumerate(SEVERITY_ORDER)}
PRIORITY_ORDER = ["critical", "high", "medium"]
PRIORITY_RANK = {name: len(PRIORITY_ORDER) - idx for idx, name in enumerate(PRIORITY_ORDER)}


def _normalize_severity(value: object) -> str:
    value = str(value).strip().lower()
    return value if value in SEVERITY_RANK else "p4"


def _normalize_env(value: object) -> str:
    value = str(value).strip().lower()
    return value if value else "unknown"


def _normalize_signature(value: object) -> str:
    return " ".join(str(value).split())


def _normalize_muted(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _normalize_ms(value: object) -> int:
    text = str(value).strip()
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def _severity_rank(value: str) -> int:
    return SEVERITY_RANK.get(value, 0)


def _load_json(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def canonicalize_alerts(rows: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for row in rows:
        alert_id = str(row.get("alert_id", "")).strip()
        if not alert_id:
            continue
        candidate = {
            "alert_id": alert_id,
            "start_ms": _normalize_ms(row.get("start_ms", 0)),
            "end_ms": _normalize_ms(row.get("end_ms", 0)),
            "severity": _normalize_severity(row.get("severity", "")),
            "env": _normalize_env(row.get("env", "")),
            "signature": _normalize_signature(row.get("signature", "")),
            "muted": _normalize_muted(row.get("muted", False)),
        }
        existing = deduped.get(alert_id)
        if existing is None:
            deduped[alert_id] = candidate
            continue
        if candidate["end_ms"] > existing["end_ms"]:
            deduped[alert_id] = candidate
            continue
        if candidate["end_ms"] < existing["end_ms"]:
            continue
        if _severity_rank(candidate["severity"]) > _severity_rank(existing["severity"]):
            deduped[alert_id] = candidate
            continue
        if _severity_rank(candidate["severity"]) < _severity_rank(existing["severity"]):
            continue
        if len(candidate["signature"]) > len(existing["signature"]):
            deduped[alert_id] = candidate
            continue
        if len(candidate["signature"]) < len(existing["signature"]):
            continue
        if candidate["env"] > existing["env"]:
            deduped[alert_id] = candidate

    canonical = list(deduped.values())
    canonical.sort(key=lambda row: (row["env"], row["start_ms"], row["alert_id"]))
    return canonical


def overlap_ms(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def freezes_by_env(freezes: list[dict]) -> dict[str, list[tuple[int, int]]]:
    by_env: dict[str, list[tuple[int, int]]] = {}
    for row in freezes:
        env = _normalize_env(row.get("env", ""))
        start = _normalize_ms(row.get("start_ms", 0))
        end = _normalize_ms(row.get("end_ms", 0))
        if end <= start:
            continue
        by_env.setdefault(env, []).append((start, end))
    compacted: dict[str, list[tuple[int, int]]] = {}
    for env in by_env:
        intervals = sorted(by_env[env])
        merged: list[list[int]] = []
        for start, end in intervals:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        compacted[env] = [(start, end) for start, end in merged]
    return compacted


def build_drift_windows(canonical: list[dict], freeze_rows: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[tuple[int, int]]]]:
    grouped: dict[str, list[dict]] = {}
    for row in canonical:
        if row["muted"]:
            continue
        grouped.setdefault(row["env"], []).append(row)

    freeze_map = freezes_by_env(freeze_rows)
    result: dict[str, list[dict]] = {}
    for env, alerts in grouped.items():
        alerts.sort(key=lambda row: (row["start_ms"], row["end_ms"], row["alert_id"]))
        windows: list[dict] = []
        current: dict | None = None
        for row in alerts:
            if current is None:
                current = {
                    "start_ms": row["start_ms"],
                    "end_ms": row["end_ms"],
                    "source_alert_ids": [row["alert_id"]],
                    "max_severity": row["severity"],
                }
                continue
            if row["start_ms"] <= current["end_ms"] + 45:
                current["end_ms"] = max(current["end_ms"], row["end_ms"])
                current["source_alert_ids"].append(row["alert_id"])
                if _severity_rank(row["severity"]) > _severity_rank(current["max_severity"]):
                    current["max_severity"] = row["severity"]
            else:
                windows.append(current)
                current = {
                    "start_ms": row["start_ms"],
                    "end_ms": row["end_ms"],
                    "source_alert_ids": [row["alert_id"]],
                    "max_severity": row["severity"],
                }
        if current is not None:
            windows.append(current)

        normalized_windows: list[dict] = []
        for window in windows:
            duration = max(window["end_ms"] - window["start_ms"], 0)
            overlap_segments: list[tuple[int, int]] = []
            for freeze_start, freeze_end in freeze_map.get(env, []):
                overlap_start = max(window["start_ms"], freeze_start)
                overlap_end = min(window["end_ms"], freeze_end)
                if overlap_end > overlap_start:
                    overlap_segments.append((overlap_start, overlap_end))
            freeze_overlap = sum(end - start for start, end in overlap_segments)
            normalized_windows.append(
                {
                    "start_ms": window["start_ms"],
                    "end_ms": window["end_ms"],
                    "duration_ms": duration,
                    "freeze_overlap_ms": freeze_overlap,
                    "freeze_segment_count": len(overlap_segments),
                    "effective_duration_ms": max(duration - freeze_overlap, 0),
                    "alert_count": len(window["source_alert_ids"]),
                    "source_alert_ids": sorted(window["source_alert_ids"]),
                    "max_severity": window["max_severity"],
                }
            )
        normalized_windows.sort(key=lambda row: row["start_ms"])
        result[env] = normalized_windows

    return {env: result[env] for env in sorted(result)}, freeze_map


def build_response_queue(drift_windows: dict[str, list[dict]]) -> list[dict]:
    queue: list[dict] = []
    for env, windows in drift_windows.items():
        for window in windows:
            if window["effective_duration_ms"] < 180:
                continue
            if window["max_severity"] not in {"p1", "p2"}:
                continue
            if (
                window["max_severity"] == "p1" and window["effective_duration_ms"] >= 280
            ) or window["effective_duration_ms"] >= 600:
                priority = "critical"
            elif window["effective_duration_ms"] >= 300 or (
                window["alert_count"] >= 3 and window["max_severity"] in {"p1", "p2"}
            ) or (
                window["freeze_segment_count"] == 0 and window["duration_ms"] >= 420
            ):
                priority = "high"
            else:
                priority = "medium"

            queue_hash = hashlib.sha1(
                (
                    f"{env}|{window['start_ms']}|{window['end_ms']}|"
                    f"{','.join(window['source_alert_ids'])}|{window['max_severity']}|{window['freeze_segment_count']}"
                ).encode("utf-8")
            ).hexdigest()[:12]

            queue.append(
                {
                    "ticket_id": f"{env}:{window['start_ms']}-{window['end_ms']}",
                    "env": env,
                    "start_ms": window["start_ms"],
                    "end_ms": window["end_ms"],
                    "duration_ms": window["duration_ms"],
                    "freeze_overlap_ms": window["freeze_overlap_ms"],
                    "freeze_segment_count": window["freeze_segment_count"],
                    "effective_duration_ms": window["effective_duration_ms"],
                    "alert_count": window["alert_count"],
                    "source_alert_ids": list(window["source_alert_ids"]),
                    "max_severity": window["max_severity"],
                    "priority": priority,
                    "queue_hash": queue_hash,
                }
            )

    queue.sort(
        key=lambda row: (
            -PRIORITY_RANK[row["priority"]],
            -row["effective_duration_ms"],
            -row["freeze_segment_count"],
            -row["alert_count"],
            row["env"],
            row["start_ms"],
        )
    )
    return queue


def build_summary(
    raw_rows: list[dict],
    canonical: list[dict],
    freeze_map: dict[str, list[tuple[int, int]]],
    drift_windows: dict[str, list[dict]],
    queue: list[dict],
) -> dict:
    severity_counts = {name: 0 for name in SEVERITY_ORDER}
    for row in canonical:
        severity_counts[_normalize_severity(row["severity"])] += 1

    total_unmuted_duration = 0
    total_overlap = 0
    total_segments = 0
    total_effective = 0
    longest_window = 0
    for windows in drift_windows.values():
        for window in windows:
            total_unmuted_duration += window["duration_ms"]
            total_overlap += window["freeze_overlap_ms"]
            total_segments += window["freeze_segment_count"]
            total_effective += window["effective_duration_ms"]
            longest_window = max(longest_window, window["duration_ms"])

    muted_excluded_count = sum(1 for row in canonical if row["muted"])
    canonical_alert_checksum = hashlib.sha256(
        "\n".join(
            (
                f"{row['alert_id']}|{row['env']}|{row['start_ms']}|{row['end_ms']}|"
                f"{row['severity']}|{1 if row['muted'] else 0}|{row['signature']}"
            )
            for row in canonical
        ).encode("utf-8")
    ).hexdigest()
    freeze_compaction_checksum = hashlib.sha256(
        "\n".join(
            f"{env}|{start}|{end}"
            for env in sorted(freeze_map)
            for start, end in freeze_map[env]
        ).encode("utf-8")
    ).hexdigest()
    queue_checksum = hashlib.sha256(
        "|".join(row["queue_hash"] for row in queue).encode("utf-8")
    ).hexdigest()

    return {
        "schema_version": SCHEMA_VERSION,
        "raw_alert_count": len(raw_rows),
        "unique_alert_ids": len(
            {str(row.get("alert_id", "")).strip() for row in raw_rows if str(row.get("alert_id", "")).strip()}
        ),
        "canonical_alert_count": len(canonical),
        "env_count": len(drift_windows),
        "severity_counts": severity_counts,
        "total_unmuted_duration_ms": total_unmuted_duration,
        "total_freeze_overlap_ms": total_overlap,
        "total_freeze_segment_count": total_segments,
        "total_effective_duration_ms": total_effective,
        "longest_window_ms": longest_window,
        "queued_window_count": len(queue),
        "muted_excluded_count": muted_excluded_count,
        "canonical_alert_checksum": canonical_alert_checksum,
        "queue_hash_checksum": queue_checksum,
        "freeze_compaction_checksum": freeze_compaction_checksum,
    }


def export_report(events: list[dict], output_dir: Path, freeze_rows: list[dict]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    canonical = canonicalize_alerts(events)
    drift_windows, freeze_map = build_drift_windows(canonical, freeze_rows)
    queue = build_response_queue(drift_windows)
    summary = build_summary(events, canonical, freeze_map, drift_windows, queue)

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (output_dir / "drift_windows.json").write_text(
        json.dumps(drift_windows, indent=2) + "\n", encoding="utf-8"
    )
    with (output_dir / "response_queue.jsonl").open("w", encoding="utf-8") as handle:
        for row in queue:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="/app/data/events.json")
    parser.add_argument("--output-dir", default="/app/output")
    args = parser.parse_args()

    events = _load_json(Path(args.input))
    freeze_rows = _load_json(FREEZE_PATH)
    export_report(events, Path(args.output_dir), freeze_rows)
    print(f"Wrote report to {args.output_dir}")


if __name__ == "__main__":
    main()
