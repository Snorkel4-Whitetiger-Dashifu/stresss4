import hashlib
import json
import subprocess
import sys
from pathlib import Path


WORKFLOW_PATH = Path("/app/workflow/export_report.py")
ORIGINAL_WORKFLOW_PATH = Path("/app/workflow/.export_report.original")
DEFAULT_INPUT = Path("/app/data/events.json")
FREEZE_PATH = Path("/app/data/change_freezes.json")
EXPECTED_FIXTURE = Path("/tests/fixtures/expected_summary.json")
ALT_INPUT = Path("/tests/fixtures/alt_events.json")

SCHEMA_VERSION = "firewall-drift-v1"
SEVERITY_ORDER = ["p1", "p2", "p3", "p4"]
SEVERITY_RANK = {name: len(SEVERITY_ORDER) - idx for idx, name in enumerate(SEVERITY_ORDER)}
PRIORITY_ORDER = ["critical", "high", "medium"]
PRIORITY_RANK = {name: len(PRIORITY_ORDER) - idx for idx, name in enumerate(PRIORITY_ORDER)}


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path):
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            rows.append(json.loads(raw))
    return rows


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


def _canonicalize_alerts(rows: list[dict]) -> list[dict]:
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


def _overlap_ms(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def _freeze_by_env(freeze_rows: list[dict]) -> dict[str, list[tuple[int, int]]]:
    grouped: dict[str, list[tuple[int, int]]] = {}
    for row in freeze_rows:
        env = _normalize_env(row.get("env", ""))
        start = _normalize_ms(row.get("start_ms", 0))
        end = _normalize_ms(row.get("end_ms", 0))
        if end <= start:
            continue
        grouped.setdefault(env, []).append((start, end))
    for env in grouped:
        grouped[env].sort()
    return grouped


def _build_windows(canonical: list[dict], freeze_rows: list[dict]) -> dict[str, list[dict]]:
    by_env: dict[str, list[dict]] = {}
    for row in canonical:
        if row["muted"]:
            continue
        by_env.setdefault(row["env"], []).append(row)

    freeze_map = _freeze_by_env(freeze_rows)
    result: dict[str, list[dict]] = {}
    for env, rows in by_env.items():
        rows.sort(key=lambda row: (row["start_ms"], row["end_ms"], row["alert_id"]))
        merged = []
        current = None
        for row in rows:
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
                merged.append(current)
                current = {
                    "start_ms": row["start_ms"],
                    "end_ms": row["end_ms"],
                    "source_alert_ids": [row["alert_id"]],
                    "max_severity": row["severity"],
                }
        if current is not None:
            merged.append(current)

        normalized = []
        for window in merged:
            duration = max(window["end_ms"] - window["start_ms"], 0)
            overlap = 0
            for freeze_start, freeze_end in freeze_map.get(env, []):
                overlap += _overlap_ms(window["start_ms"], window["end_ms"], freeze_start, freeze_end)
            normalized.append(
                {
                    "start_ms": window["start_ms"],
                    "end_ms": window["end_ms"],
                    "duration_ms": duration,
                    "freeze_overlap_ms": overlap,
                    "effective_duration_ms": max(duration - overlap, 0),
                    "alert_count": len(window["source_alert_ids"]),
                    "source_alert_ids": sorted(window["source_alert_ids"]),
                    "max_severity": window["max_severity"],
                }
            )

        normalized.sort(key=lambda row: row["start_ms"])
        result[env] = normalized

    return {env: result[env] for env in sorted(result)}


def _build_queue(windows: dict[str, list[dict]]) -> list[dict]:
    queue = []
    for env, env_windows in windows.items():
        for window in env_windows:
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
            ):
                priority = "high"
            else:
                priority = "medium"

            queue_hash = hashlib.sha1(
                (
                    f"{env}|{window['start_ms']}|{window['end_ms']}|"
                    f"{','.join(window['source_alert_ids'])}"
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
            -row["alert_count"],
            row["env"],
            row["start_ms"],
        )
    )
    return queue


def _build_summary(raw_rows: list[dict], canonical: list[dict], windows: dict[str, list[dict]], queue: list[dict]) -> dict:
    severity_counts = {name: 0 for name in SEVERITY_ORDER}
    for row in canonical:
        severity_counts[row["severity"]] += 1

    total_duration = 0
    total_overlap = 0
    total_effective = 0
    longest_window = 0
    for env_windows in windows.values():
        for window in env_windows:
            total_duration += window["duration_ms"]
            total_overlap += window["freeze_overlap_ms"]
            total_effective += window["effective_duration_ms"]
            longest_window = max(longest_window, window["duration_ms"])

    queue_checksum = hashlib.sha256("|".join(row["queue_hash"] for row in queue).encode("utf-8")).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "raw_alert_count": len(raw_rows),
        "unique_alert_ids": len({str(row.get("alert_id", "")).strip() for row in raw_rows if str(row.get("alert_id", "")).strip()}),
        "canonical_alert_count": len(canonical),
        "env_count": len(windows),
        "severity_counts": severity_counts,
        "total_unmuted_duration_ms": total_duration,
        "total_freeze_overlap_ms": total_overlap,
        "total_effective_duration_ms": total_effective,
        "longest_window_ms": longest_window,
        "queued_window_count": len(queue),
        "muted_excluded_count": sum(1 for row in canonical if row["muted"]),
        "queue_hash_checksum": queue_checksum,
    }


def _expected_from_input(input_path: Path) -> dict:
    raw_rows = _load_json(input_path)
    freeze_rows = _load_json(FREEZE_PATH)
    canonical = _canonicalize_alerts(raw_rows)
    windows = _build_windows(canonical, freeze_rows)
    queue = _build_queue(windows)
    summary = _build_summary(raw_rows, canonical, windows, queue)
    return {"summary": summary, "windows": windows, "queue": queue}


def _run_pipeline(tmp_path: Path, script_path: Path = WORKFLOW_PATH, input_path: Path = DEFAULT_INPUT):
    out_dir = tmp_path / "output"
    subprocess.run(
        [sys.executable, str(script_path), "--input", str(input_path), "--output-dir", str(out_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = _load_json(out_dir / "summary.json")
    windows = _load_json(out_dir / "drift_windows.json")
    queue = _load_jsonl(out_dir / "response_queue.jsonl")
    return out_dir, summary, windows, queue


def _summary_hash(summary: dict) -> str:
    return hashlib.sha256(json.dumps(summary, sort_keys=True).encode("utf-8")).hexdigest()


def _executable_text() -> str:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    lines = []
    in_docstring = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def test_summary_schema(tmp_path: Path):
    _, summary, _, _ = _run_pipeline(tmp_path)
    assert set(summary) == {
        "schema_version",
        "raw_alert_count",
        "unique_alert_ids",
        "canonical_alert_count",
        "env_count",
        "severity_counts",
        "total_unmuted_duration_ms",
        "total_freeze_overlap_ms",
        "total_effective_duration_ms",
        "longest_window_ms",
        "queued_window_count",
        "muted_excluded_count",
        "queue_hash_checksum",
    }
    assert summary["schema_version"] == SCHEMA_VERSION
    assert list(summary["severity_counts"]) == SEVERITY_ORDER
    assert len(summary["queue_hash_checksum"]) == 64


def test_windows_schema(tmp_path: Path):
    _, _, windows, _ = _run_pipeline(tmp_path)
    for env, env_windows in windows.items():
        assert env == env.strip().lower()
        for row in env_windows:
            assert set(row) == {
                "start_ms",
                "end_ms",
                "duration_ms",
                "freeze_overlap_ms",
                "effective_duration_ms",
                "alert_count",
                "source_alert_ids",
                "max_severity",
            }


def test_queue_required_fields(tmp_path: Path):
    _, _, _, queue = _run_pipeline(tmp_path)
    for row in queue:
        assert set(row) == {
            "ticket_id",
            "env",
            "start_ms",
            "end_ms",
            "duration_ms",
            "freeze_overlap_ms",
            "effective_duration_ms",
            "alert_count",
            "source_alert_ids",
            "max_severity",
            "priority",
            "queue_hash",
        }
        assert row["priority"] in PRIORITY_RANK
        assert len(row["queue_hash"]) == 12


def test_outputs_match_expected_computation(tmp_path: Path):
    _, summary, windows, queue = _run_pipeline(tmp_path)
    expected = _expected_from_input(DEFAULT_INPUT)
    assert summary == expected["summary"]
    assert windows == expected["windows"]
    assert queue == expected["queue"]


def test_priority_rules(tmp_path: Path):
    _, _, _, queue = _run_pipeline(tmp_path)
    for row in queue:
        if (row["max_severity"] == "p1" and row["effective_duration_ms"] >= 280) or row["effective_duration_ms"] >= 600:
            assert row["priority"] == "critical"
        elif row["effective_duration_ms"] >= 300 or (
            row["alert_count"] >= 3 and row["max_severity"] in {"p1", "p2"}
        ):
            assert row["priority"] == "high"
        else:
            assert row["priority"] == "medium"


def test_queue_sorted(tmp_path: Path):
    _, _, _, queue = _run_pipeline(tmp_path)
    assert queue == sorted(
        queue,
        key=lambda row: (
            -PRIORITY_RANK[row["priority"]],
            -row["effective_duration_ms"],
            -row["alert_count"],
            row["env"],
            row["start_ms"],
        ),
    )


def test_windows_sorted_and_envs_sorted(tmp_path: Path):
    _, _, windows, _ = _run_pipeline(tmp_path)
    assert list(windows) == sorted(windows)
    for env_windows in windows.values():
        starts = [row["start_ms"] for row in env_windows]
        assert starts == sorted(starts)


def test_freeze_math_consistency(tmp_path: Path):
    _, _, windows, _ = _run_pipeline(tmp_path)
    for env_windows in windows.values():
        for row in env_windows:
            assert row["freeze_overlap_ms"] >= 0
            assert row["effective_duration_ms"] == max(row["duration_ms"] - row["freeze_overlap_ms"], 0)


def test_output_dir_contains_exactly_three_files(tmp_path: Path):
    out_dir, _, _, _ = _run_pipeline(tmp_path)
    names = sorted(path.name for path in out_dir.iterdir() if path.is_file())
    assert names == ["drift_windows.json", "response_queue.jsonl", "summary.json"]


def test_broken_snapshot_is_wrong(tmp_path: Path):
    fixture = _load_json(EXPECTED_FIXTURE)
    broken_dir, broken_summary, _, broken_queue = _run_pipeline(tmp_path, script_path=ORIGINAL_WORKFLOW_PATH)

    assert _summary_hash(broken_summary) == fixture["broken_pipeline_sha256"]
    assert len(broken_queue) == fixture["broken_queue_count"]
    assert [row.get("ticket_id") for row in broken_queue] == fixture["broken_queue_ticket_ids"]

    fixed_dir, fixed_summary, _, _ = _run_pipeline(tmp_path / "fixed", script_path=WORKFLOW_PATH)
    assert _summary_hash(fixed_summary) != _summary_hash(broken_summary)
    assert fixed_dir != broken_dir


def test_pipeline_rerun_idempotent(tmp_path: Path):
    out_a, summary_a, windows_a, queue_a = _run_pipeline(tmp_path / "a")
    out_b, summary_b, windows_b, queue_b = _run_pipeline(tmp_path / "b")
    assert summary_a == summary_b
    assert windows_a == windows_b
    assert queue_a == queue_b
    assert out_a != out_b


def test_pipeline_supports_alternate_input(tmp_path: Path):
    out_dir, _, _, _ = _run_pipeline(tmp_path, input_path=ALT_INPUT)
    assert (out_dir / "summary.json").exists()
    assert (out_dir / "drift_windows.json").exists()
    assert (out_dir / "response_queue.jsonl").exists()


def test_alternate_input_expected_values(tmp_path: Path):
    fixture = _load_json(EXPECTED_FIXTURE)
    _, summary, _, queue = _run_pipeline(tmp_path, input_path=ALT_INPUT)
    alt_expected = fixture["alternate_expected"]
    assert summary["raw_alert_count"] == alt_expected["raw_alert_count"]
    assert summary["unique_alert_ids"] == alt_expected["unique_alert_ids"]
    assert summary["canonical_alert_count"] == alt_expected["canonical_alert_count"]
    assert summary["env_count"] == alt_expected["env_count"]
    assert summary["queued_window_count"] == alt_expected["queued_window_count"]
    assert summary["muted_excluded_count"] == alt_expected["muted_excluded_count"]
    assert [row["priority"] for row in queue] == alt_expected["priority_sequence"]


def test_pipeline_supports_custom_output_dir(tmp_path: Path):
    custom = tmp_path / "custom-output"
    subprocess.run(
        [sys.executable, str(WORKFLOW_PATH), "--input", str(DEFAULT_INPUT), "--output-dir", str(custom)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (custom / "summary.json").exists()


def test_cli_defaults_work_and_match_explicit_run(tmp_path: Path):
    default_out = tmp_path / "default"
    explicit_out = tmp_path / "explicit"
    subprocess.run([sys.executable, str(WORKFLOW_PATH)], check=True, capture_output=True, text=True, cwd="/app")
    default_summary = _load_json(Path("/app/output/summary.json"))

    subprocess.run(
        [sys.executable, str(WORKFLOW_PATH), "--input", str(DEFAULT_INPUT), "--output-dir", str(explicit_out)],
        check=True,
        capture_output=True,
        text=True,
    )
    explicit_summary = _load_json(explicit_out / "summary.json")
    assert default_summary == explicit_summary
    assert default_out.exists() or Path("/app/output").exists()


def test_freeze_source_path_affects_output(tmp_path: Path):
    original = FREEZE_PATH.read_text(encoding="utf-8")
    try:
        _, summary_a, _, _ = _run_pipeline(tmp_path / "a")
        FREEZE_PATH.write_text("[]\n", encoding="utf-8")
        _, summary_b, _, _ = _run_pipeline(tmp_path / "b")
        assert summary_a["total_freeze_overlap_ms"] != summary_b["total_freeze_overlap_ms"]
    finally:
        FREEZE_PATH.write_text(original, encoding="utf-8")


def test_muted_string_coercion_variants_are_exercised(tmp_path: Path):
    rows = [
        {"alert_id": "m1", "start_ms": 10, "end_ms": 300, "severity": "p1", "env": "prod", "signature": "a", "muted": "YES"},
        {"alert_id": "m2", "start_ms": 20, "end_ms": 320, "severity": "p1", "env": "prod", "signature": "b", "muted": "false"},
    ]
    input_path = tmp_path / "muted.json"
    input_path.write_text(json.dumps(rows), encoding="utf-8")
    _, summary, windows, _ = _run_pipeline(tmp_path, input_path=input_path)
    assert summary["canonical_alert_count"] == 2
    assert summary["muted_excluded_count"] == 1
    assert sum(window["alert_count"] for env_windows in windows.values() for window in env_windows) == 1


def test_end_ms_coercion_invalid_to_zero(tmp_path: Path):
    rows = [
        {"alert_id": "e1", "start_ms": 100, "end_ms": "invalid", "severity": "p1", "env": "prod", "signature": "a", "muted": False},
        {"alert_id": "e1", "start_ms": 100, "end_ms": "400", "severity": "p1", "env": "prod", "signature": "b", "muted": False}
    ]
    input_path = tmp_path / "bad-end.json"
    input_path.write_text(json.dumps(rows), encoding="utf-8")
    _, windows, queue = None, None, None
    _, _, windows, queue = _run_pipeline(tmp_path, input_path=input_path)
    assert list(windows) == ["prod"]
    assert windows["prod"][0]["end_ms"] == 400
    assert queue[0]["end_ms"] == 400


def test_severity_strip_lower_normalization_is_exercised(tmp_path: Path):
    rows = [
        {"alert_id": "s1", "start_ms": 100, "end_ms": 500, "severity": " P1 ", "env": "prod", "signature": "a", "muted": False}
    ]
    input_path = tmp_path / "sev.json"
    input_path.write_text(json.dumps(rows), encoding="utf-8")
    _, _, _, queue = _run_pipeline(tmp_path, input_path=input_path)
    assert queue[0]["max_severity"] == "p1"
    assert queue[0]["priority"] == "critical"


def test_dedupe_tie_break_prefers_longer_signature_same_end(tmp_path: Path):
    rows = [
        {"alert_id": "d1", "start_ms": 100, "end_ms": 500, "severity": "p2", "env": "prod", "signature": "short", "muted": False},
        {"alert_id": "d1", "start_ms": 100, "end_ms": 500, "severity": "p2", "env": "prod", "signature": "longer signature", "muted": False},
    ]
    canonical = _canonicalize_alerts(rows)
    assert canonical[0]["signature"] == "longer signature"


def test_dedupe_tie_break_uses_env_when_signature_lengths_equal(tmp_path: Path):
    rows = [
        {"alert_id": "d2", "start_ms": 100, "end_ms": 500, "severity": "p2", "env": "aaa", "signature": "equal len", "muted": False},
        {"alert_id": "d2", "start_ms": 100, "end_ms": 500, "severity": "p2", "env": "zzz", "signature": "equal len", "muted": False},
    ]
    canonical = _canonicalize_alerts(rows)
    assert canonical[0]["env"] == "zzz"


def test_signature_whitespace_is_collapsed_before_dedupe_length_tie_break():
    rows = [
        {
            "alert_id": "ws1",
            "start_ms": 100,
            "end_ms": 500,
            "severity": "p2",
            "env": "prod",
            "signature": "a     b",
            "muted": False,
        },
        {
            "alert_id": "ws1",
            "start_ms": 100,
            "end_ms": 500,
            "severity": "p2",
            "env": "prod",
            "signature": "abcd",
            "muted": False,
        },
    ]
    canonical = _canonicalize_alerts(rows)
    assert canonical[0]["signature"] == "abcd"


def test_dedupe_full_tie_keeps_first_seen_row():
    rows = [
        {
            "alert_id": "tie1",
            "start_ms": 100,
            "end_ms": 500,
            "severity": "p2",
            "env": "prod",
            "signature": "aa bb",
            "muted": False,
        },
        {
            "alert_id": "tie1",
            "start_ms": 200,
            "end_ms": 500,
            "severity": "p2",
            "env": "prod",
            "signature": "cc dd",
            "muted": True,
        },
    ]
    canonical = _canonicalize_alerts(rows)
    assert canonical[0]["start_ms"] == 100
    assert canonical[0]["muted"] is False


def test_merge_gap_45_and_freeze_overlap_math_on_custom_data(tmp_path: Path):
    rows = [
        {"alert_id": "g1", "start_ms": 1000, "end_ms": 1300, "severity": "p2", "env": "lab", "signature": "a", "muted": False},
        {"alert_id": "g2", "start_ms": 1345, "end_ms": 1600, "severity": "p2", "env": "lab", "signature": "b", "muted": False},
    ]
    freezes = [{"env": "lab", "start_ms": 1200, "end_ms": 1500}]
    input_path = tmp_path / "gap.json"
    input_path.write_text(json.dumps(rows), encoding="utf-8")
    original = FREEZE_PATH.read_text(encoding="utf-8")
    try:
        FREEZE_PATH.write_text(json.dumps(freezes), encoding="utf-8")
        _, _, windows, queue = _run_pipeline(tmp_path, input_path=input_path)
    finally:
        FREEZE_PATH.write_text(original, encoding="utf-8")

    assert len(windows["lab"]) == 1
    window = windows["lab"][0]
    assert window["duration_ms"] == 600
    assert window["freeze_overlap_ms"] == 300
    assert window["effective_duration_ms"] == 300
    assert queue[0]["priority"] == "high"


def test_response_queue_jsonl_is_compact(tmp_path: Path):
    out_dir, _, _, _ = _run_pipeline(tmp_path)
    raw_lines = (out_dir / "response_queue.jsonl").read_text(encoding="utf-8").splitlines()
    for line in raw_lines:
        if not line.strip():
            continue
        assert ": " not in line
        parsed = json.loads(line)
        assert json.dumps(parsed, separators=(",", ":")) == line


def test_queue_hash_checksum_matches_queue_rows(tmp_path: Path):
    _, summary, _, queue = _run_pipeline(tmp_path)
    expected_checksum = hashlib.sha256("|".join(row["queue_hash"] for row in queue).encode("utf-8")).hexdigest()
    assert summary["queue_hash_checksum"] == expected_checksum


def test_pipeline_does_not_reference_test_artifacts():
    executable = _executable_text()
    forbidden = ["/tests", "expected_summary.json", "fixtures/alt_events.json"]
    for token in forbidden:
        assert token not in executable
