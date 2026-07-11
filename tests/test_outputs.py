"""Verifier tests for firewall drift compiler task."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

WORKFLOW_PATH = Path("/app/workflow/export_report.py")
ORIGINAL_WORKFLOW_PATH = Path("/app/workflow/.export_report.original")
DEFAULT_INPUT = Path("/app/data/events.json")
FREEZE_PATH = Path("/app/data/change_freezes.json")
REOPEN_PATH = Path("/app/data/reopen_windows.json")
ROTATION_PATH = Path("/app/data/rotation_windows.json")
EXPECTED_FIXTURE = Path("/tests/fixtures/expected_summary.json")
ALT_INPUT = Path("/tests/fixtures/alt_events.json")

SEVERITY_ORDER = ["p1", "p2", "p3", "p4"]
PRIORITY_ORDER = ["critical", "high", "medium"]
PRIORITY_RANK = {name: len(PRIORITY_ORDER) - idx for idx, name in enumerate(PRIORITY_ORDER)}

FIXTURE = json.loads(EXPECTED_FIXTURE.read_text())


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path):
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            rows.append(json.loads(raw))
    return rows


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _run_pipeline(tmp_path: Path, script_path: Path = WORKFLOW_PATH, input_path: Path = DEFAULT_INPUT):
    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(script_path), "--input", str(input_path), "--output-dir", str(out_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    summary = _load_json(out_dir / "summary.json")
    windows = _load_json(out_dir / "drift_windows.json")
    queue = _load_jsonl(out_dir / "response_queue.jsonl")
    return out_dir, summary, windows, queue


@pytest.fixture(scope="session")
def primary_outputs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("primary")
    return _run_pipeline(tmp)


def test_cli_exists():
    assert WORKFLOW_PATH.exists()


def test_output_dir_contains_exactly_three_files(primary_outputs):
    out_dir, _, _, _ = primary_outputs
    names = sorted(path.name for path in out_dir.iterdir() if path.is_file())
    assert names == ["drift_windows.json", "response_queue.jsonl", "summary.json"]


def test_primary_summary_matches_fixture(primary_outputs):
    _, summary, _, _ = primary_outputs
    assert summary == FIXTURE["primary"]["summary"]


def test_primary_windows_matches_fixture(primary_outputs):
    _, _, windows, _ = primary_outputs
    assert windows == FIXTURE["primary"]["windows"]


def test_primary_queue_matches_fixture(primary_outputs):
    _, _, _, queue = primary_outputs
    assert queue == FIXTURE["primary"]["queue_rows"]


def test_summary_schema(primary_outputs):
    _, summary, _, _ = primary_outputs
    assert set(summary) == {
        "schema_version",
        "raw_alert_count",
        "unique_alert_ids",
        "canonical_alert_count",
        "env_count",
        "severity_counts",
        "total_unmuted_duration_ms",
        "total_freeze_overlap_ms",
        "total_freeze_segment_count",
        "total_effective_duration_ms",
        "total_reopen_overlap_ms",
        "total_reopen_segment_count",
        "total_risk_adjusted_duration_ms",
        "total_rotation_overlap_ms",
        "total_rotation_segment_count",
        "total_dispatchable_duration_ms",
        "longest_window_ms",
        "queued_window_count",
        "muted_excluded_count",
        "max_stability_pressure_score",
        "max_volatility_index",
        "canonical_alert_checksum",
        "queue_hash_checksum",
        "freeze_compaction_checksum",
        "reopen_compaction_checksum",
        "rotation_compaction_checksum",
        "window_digest_checksum",
    }
    assert summary["schema_version"] == "firewall-drift-v1"
    assert list(summary["severity_counts"]) == SEVERITY_ORDER
    assert len(summary["canonical_alert_checksum"]) == 64
    assert len(summary["queue_hash_checksum"]) == 64
    assert len(summary["freeze_compaction_checksum"]) == 64
    assert len(summary["reopen_compaction_checksum"]) == 64
    assert len(summary["rotation_compaction_checksum"]) == 64
    assert len(summary["window_digest_checksum"]) == 64


def test_windows_schema_and_sorting(primary_outputs):
    _, _, windows, _ = primary_outputs
    expected_keys = {
        "start_ms",
        "end_ms",
        "duration_ms",
        "freeze_overlap_ms",
        "freeze_segment_count",
        "effective_duration_ms",
        "reopen_overlap_ms",
        "reopen_segment_count",
        "risk_adjusted_duration_ms",
        "rotation_overlap_ms",
        "rotation_segment_count",
        "dispatchable_duration_ms",
        "alert_count",
        "source_alert_ids",
        "max_severity",
    }
    assert list(windows) == sorted(windows)
    for env_windows in windows.values():
        starts = [row["start_ms"] for row in env_windows]
        assert starts == sorted(starts)
        for row in env_windows:
            assert set(row) == expected_keys
            assert row["duration_ms"] == max(row["end_ms"] - row["start_ms"], 0)
            assert row["effective_duration_ms"] == max(
                row["duration_ms"] - row["freeze_overlap_ms"], 0
            )
            assert row["risk_adjusted_duration_ms"] == max(
                row["effective_duration_ms"] - (row["reopen_overlap_ms"] // 2), 0
            )
            assert row["dispatchable_duration_ms"] == max(
                row["risk_adjusted_duration_ms"] - (row["rotation_overlap_ms"] // 3), 0
            )
            assert row["source_alert_ids"] == sorted(row["source_alert_ids"])


def test_queue_required_fields(primary_outputs):
    _, _, _, queue = primary_outputs
    expected_keys = {
        "ticket_id",
        "env",
        "start_ms",
        "end_ms",
        "duration_ms",
        "freeze_overlap_ms",
        "freeze_segment_count",
        "effective_duration_ms",
        "reopen_overlap_ms",
        "reopen_segment_count",
        "risk_adjusted_duration_ms",
        "rotation_overlap_ms",
        "rotation_segment_count",
        "dispatchable_duration_ms",
        "alert_count",
        "source_alert_ids",
        "max_severity",
        "stability_pressure_score",
        "volatility_index",
        "priority",
        "queue_hash",
        "window_digest",
    }
    for row in queue:
        assert set(row) == expected_keys
        assert row["priority"] in PRIORITY_RANK
        assert len(row["queue_hash"]) == 12
        assert len(row["window_digest"]) == 10


def test_priority_rules(primary_outputs):
    _, _, _, queue = primary_outputs
    for row in queue:
        if (
            row["max_severity"] == "p1" and row["dispatchable_duration_ms"] >= 250
        ) or row["dispatchable_duration_ms"] >= 520 or row["volatility_index"] >= 18:
            assert row["priority"] == "critical"
        elif row["dispatchable_duration_ms"] >= 280 or (
            row["alert_count"] >= 3 and row["max_severity"] in {"p1", "p2"}
        ) or (
            row["rotation_segment_count"] == 0
            and row["risk_adjusted_duration_ms"] >= 340
        ) or (row["reopen_segment_count"] == 0 and row["duration_ms"] >= 420):
            assert row["priority"] == "high"
        else:
            assert row["priority"] == "medium"


def test_queue_sorted(primary_outputs):
    _, _, _, queue = primary_outputs
    assert queue == sorted(
        queue,
        key=lambda row: (
            -PRIORITY_RANK[row["priority"]],
            -row["dispatchable_duration_ms"],
            -row["volatility_index"],
            -row["risk_adjusted_duration_ms"],
            -row["freeze_segment_count"],
            -row["alert_count"],
            row["env"],
            row["start_ms"],
        ),
    )


def test_response_queue_jsonl_compact(primary_outputs):
    out_dir, _, _, _ = primary_outputs
    for line in (out_dir / "response_queue.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        assert ": " not in line
        parsed = json.loads(line)
        assert json.dumps(parsed, separators=(",", ":")) == line


def test_summary_math_consistency(primary_outputs):
    _, summary, windows, _ = primary_outputs
    duration_total = 0
    overlap_total = 0
    segment_total = 0
    effective_total = 0
    reopen_overlap_total = 0
    reopen_segment_total = 0
    risk_adjusted_total = 0
    rotation_overlap_total = 0
    rotation_segment_total = 0
    dispatchable_total = 0
    longest = 0
    for env_windows in windows.values():
        for row in env_windows:
            duration_total += row["duration_ms"]
            overlap_total += row["freeze_overlap_ms"]
            segment_total += row["freeze_segment_count"]
            effective_total += row["effective_duration_ms"]
            reopen_overlap_total += row["reopen_overlap_ms"]
            reopen_segment_total += row["reopen_segment_count"]
            risk_adjusted_total += row["risk_adjusted_duration_ms"]
            rotation_overlap_total += row["rotation_overlap_ms"]
            rotation_segment_total += row["rotation_segment_count"]
            dispatchable_total += row["dispatchable_duration_ms"]
            longest = max(longest, row["duration_ms"])
    assert summary["total_unmuted_duration_ms"] == duration_total
    assert summary["total_freeze_overlap_ms"] == overlap_total
    assert summary["total_freeze_segment_count"] == segment_total
    assert summary["total_effective_duration_ms"] == effective_total
    assert summary["total_reopen_overlap_ms"] == reopen_overlap_total
    assert summary["total_reopen_segment_count"] == reopen_segment_total
    assert summary["total_risk_adjusted_duration_ms"] == risk_adjusted_total
    assert summary["total_rotation_overlap_ms"] == rotation_overlap_total
    assert summary["total_rotation_segment_count"] == rotation_segment_total
    assert summary["total_dispatchable_duration_ms"] == dispatchable_total
    assert summary["longest_window_ms"] == longest


def test_original_snapshot_preserved():
    assert ORIGINAL_WORKFLOW_PATH.exists()
    digest = hashlib.sha256(ORIGINAL_WORKFLOW_PATH.read_bytes()).hexdigest()
    assert digest == FIXTURE["broken_pipeline_sha256"]


def test_broken_snapshot_is_wrong(tmp_path: Path):
    _, broken_summary, _, broken_queue = _run_pipeline(tmp_path, script_path=ORIGINAL_WORKFLOW_PATH)
    broken_summary_hash = hashlib.sha256(
        json.dumps(broken_summary, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert broken_summary_hash == FIXTURE["broken_summary_sha256"]
    assert len(broken_queue) == FIXTURE["broken_queue_count"]
    assert [row.get("ticket_id") for row in broken_queue] == FIXTURE["broken_queue_ticket_ids"]
    assert broken_queue != FIXTURE["primary"]["queue_rows"]


def test_pipeline_rerun_idempotent(tmp_path: Path):
    _, summary_a, windows_a, queue_a = _run_pipeline(tmp_path / "a")
    _, summary_b, windows_b, queue_b = _run_pipeline(tmp_path / "b")
    assert summary_a == summary_b
    assert windows_a == windows_b
    assert queue_a == queue_b


def test_pipeline_supports_alternate_input(tmp_path: Path):
    _, summary, windows, queue = _run_pipeline(tmp_path, input_path=ALT_INPUT)
    assert summary == FIXTURE["alternate"]["summary"]
    assert windows == FIXTURE["alternate"]["windows"]
    assert queue == FIXTURE["alternate"]["queue_rows"]


def test_pipeline_supports_custom_output_dir(tmp_path: Path):
    custom = tmp_path / "custom-output"
    subprocess.run(
        [sys.executable, str(WORKFLOW_PATH), "--input", str(DEFAULT_INPUT), "--output-dir", str(custom)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (custom / "summary.json").exists()
    assert (custom / "drift_windows.json").exists()
    assert (custom / "response_queue.jsonl").exists()


def test_cli_defaults_work_and_match_explicit_run(tmp_path: Path):
    explicit_out = tmp_path / "explicit"
    explicit_out.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, str(WORKFLOW_PATH), "--input", str(DEFAULT_INPUT), "--output-dir", str(explicit_out)],
        check=True,
        capture_output=True,
        text=True,
    )
    explicit_summary = _load_json(explicit_out / "summary.json")

    subprocess.run([sys.executable, str(WORKFLOW_PATH)], check=True, capture_output=True, text=True)
    default_summary = _load_json(Path("/app/output/summary.json"))
    assert default_summary == explicit_summary


def test_freeze_source_path_affects_output(tmp_path: Path):
    original = FREEZE_PATH.read_text(encoding="utf-8")
    try:
        _, summary_a, _, _ = _run_pipeline(tmp_path / "a")
        FREEZE_PATH.write_text("[]\n", encoding="utf-8")
        _, summary_b, _, _ = _run_pipeline(tmp_path / "b")
        assert summary_a["total_freeze_overlap_ms"] != summary_b["total_freeze_overlap_ms"]
        assert summary_a["freeze_compaction_checksum"] != summary_b["freeze_compaction_checksum"]
        assert (
            summary_a["total_risk_adjusted_duration_ms"]
            != summary_b["total_risk_adjusted_duration_ms"]
        )
    finally:
        FREEZE_PATH.write_text(original, encoding="utf-8")


def test_reopen_source_path_affects_output(tmp_path: Path):
    original = REOPEN_PATH.read_text(encoding="utf-8")
    try:
        _, summary_a, _, queue_a = _run_pipeline(tmp_path / "a")
        REOPEN_PATH.write_text("[]\n", encoding="utf-8")
        _, summary_b, _, queue_b = _run_pipeline(tmp_path / "b")
        assert summary_a["total_reopen_overlap_ms"] > 0
        assert summary_b["total_reopen_overlap_ms"] == 0
        assert summary_a["reopen_compaction_checksum"] != summary_b["reopen_compaction_checksum"]
        assert summary_a["window_digest_checksum"] != summary_b["window_digest_checksum"]
        assert queue_a != queue_b
    finally:
        REOPEN_PATH.write_text(original, encoding="utf-8")


def test_rotation_source_path_affects_output(tmp_path: Path):
    original = ROTATION_PATH.read_text(encoding="utf-8")
    try:
        _, summary_a, _, queue_a = _run_pipeline(tmp_path / "a")
        ROTATION_PATH.write_text("[]\n", encoding="utf-8")
        _, summary_b, _, queue_b = _run_pipeline(tmp_path / "b")
        assert summary_a["total_rotation_overlap_ms"] > 0
        assert summary_b["total_rotation_overlap_ms"] == 0
        assert (
            summary_a["rotation_compaction_checksum"]
            != summary_b["rotation_compaction_checksum"]
        )
        assert summary_a["window_digest_checksum"] != summary_b["window_digest_checksum"]
        assert queue_a != queue_b
    finally:
        ROTATION_PATH.write_text(original, encoding="utf-8")


def test_compacted_freeze_segments_are_used(tmp_path: Path):
    original = FREEZE_PATH.read_text(encoding="utf-8")
    try:
        custom_freezes = [
            {"env": "lab", "start_ms": 1200, "end_ms": 1300},
            {"env": "lab", "start_ms": 1250, "end_ms": 1400},
            {"env": "lab", "start_ms": 1400, "end_ms": 1450},
            {"env": "lab", "start_ms": 1600, "end_ms": 1650},
        ]
        FREEZE_PATH.write_text(json.dumps(custom_freezes), encoding="utf-8")
        rows = [
            {
                "alert_id": "m1",
                "start_ms": 1100,
                "end_ms": 1700,
                "severity": "p2",
                "env": "lab",
                "signature": "x",
                "muted": False,
            }
        ]
        input_path = tmp_path / "edge.json"
        _write_json(input_path, rows)
        _, _, windows, queue = _run_pipeline(tmp_path / "run", input_path=input_path)
        window = windows["lab"][0]
        assert window["freeze_overlap_ms"] == 300
        assert window["freeze_segment_count"] == 2
        assert queue[0]["freeze_segment_count"] == 2
    finally:
        FREEZE_PATH.write_text(original, encoding="utf-8")


def test_reopen_compaction_and_scope_are_used(tmp_path: Path):
    original_reopen = REOPEN_PATH.read_text(encoding="utf-8")
    try:
        reopen_rows = [
            {"env": "lab", "severity_scope": "all", "start_ms": 150, "end_ms": 200},
            {"env": "lab", "severity_scope": "all", "start_ms": 200, "end_ms": 240},
            {"env": "lab", "severity_scope": "p1", "start_ms": 260, "end_ms": 320},
            {"env": "lab", "severity_scope": "debug", "start_ms": 0, "end_ms": 1},
        ]
        REOPEN_PATH.write_text(json.dumps(reopen_rows) + "\n", encoding="utf-8")
        rows = [
            {
                "alert_id": "r1",
                "start_ms": 100,
                "end_ms": 400,
                "severity": "p1",
                "env": "lab",
                "signature": "alpha",
                "muted": False,
            },
            {
                "alert_id": "r2",
                "start_ms": 500,
                "end_ms": 800,
                "severity": "p2",
                "env": "lab",
                "signature": "beta",
                "muted": False,
            },
        ]
        input_path = tmp_path / "reopen_scope.json"
        _write_json(input_path, rows)
        _, summary, windows, queue = _run_pipeline(tmp_path / "run", input_path=input_path)
        first = windows["lab"][0]
        second = windows["lab"][1]
        assert first["reopen_overlap_ms"] == 150
        assert first["reopen_segment_count"] == 2
        assert first["risk_adjusted_duration_ms"] == 225
        assert second["reopen_overlap_ms"] == 0
        assert summary["total_reopen_overlap_ms"] == 150
        assert summary["total_reopen_segment_count"] == 2
        assert [row["ticket_id"] for row in queue] == ["lab:500-800", "lab:100-400"]
    finally:
        REOPEN_PATH.write_text(original_reopen, encoding="utf-8")


def test_rotation_compaction_and_scope_are_used(tmp_path: Path):
    original_rotation = ROTATION_PATH.read_text(encoding="utf-8")
    try:
        rotation_rows = [
            {"env": "lab", "severity_scope": "all", "start_ms": 130, "end_ms": 180},
            {"env": "lab", "severity_scope": "all", "start_ms": 178, "end_ms": 240},
            {"env": "lab", "severity_scope": "p1", "start_ms": 260, "end_ms": 310},
            {"env": "lab", "severity_scope": "debug", "start_ms": 0, "end_ms": 1},
        ]
        ROTATION_PATH.write_text(json.dumps(rotation_rows) + "\n", encoding="utf-8")
        rows = [
            {
                "alert_id": "z1",
                "start_ms": 100,
                "end_ms": 400,
                "severity": "p1",
                "env": "lab",
                "signature": "alpha",
                "muted": False,
            },
            {
                "alert_id": "z2",
                "start_ms": 500,
                "end_ms": 820,
                "severity": "p2",
                "env": "lab",
                "signature": "beta",
                "muted": False,
            },
        ]
        input_path = tmp_path / "rotation_scope.json"
        _write_json(input_path, rows)
        _, summary, windows, queue = _run_pipeline(tmp_path / "run", input_path=input_path)
        first = windows["lab"][0]
        second = windows["lab"][1]
        assert first["rotation_overlap_ms"] == 160
        assert first["rotation_segment_count"] == 2
        assert first["dispatchable_duration_ms"] == max(
            first["risk_adjusted_duration_ms"] - (160 // 3), 0
        )
        assert second["rotation_overlap_ms"] == 0
        assert summary["total_rotation_overlap_ms"] == 160
        assert summary["total_rotation_segment_count"] == 2
        assert queue[0]["ticket_id"] == "lab:500-820"
    finally:
        ROTATION_PATH.write_text(original_rotation, encoding="utf-8")


def test_tie_break_full_tie_keeps_first_seen(tmp_path: Path):
    rows = [
        {
            "alert_id": "t1",
            "start_ms": 100,
            "end_ms": 500,
            "severity": "p2",
            "env": "prod",
            "signature": "ab cd",
            "muted": False,
        },
        {
            "alert_id": "t1",
            "start_ms": 900,
            "end_ms": 500,
            "severity": "p2",
            "env": "prod",
            "signature": "ef gh",
            "muted": True,
        },
    ]
    input_path = tmp_path / "tie.json"
    _write_json(input_path, rows)
    _, summary, windows, _ = _run_pipeline(tmp_path / "run", input_path=input_path)
    assert summary["canonical_alert_count"] == 1
    assert summary["muted_excluded_count"] == 0
    assert windows["prod"][0]["start_ms"] == 100


def test_muted_truthiness_non_string_non_bool(tmp_path: Path):
    rows = [
        {
            "alert_id": "m1",
            "start_ms": 100,
            "end_ms": 500,
            "severity": "p1",
            "env": "prod",
            "signature": "x",
            "muted": 0,
        },
        {
            "alert_id": "m2",
            "start_ms": 100,
            "end_ms": 500,
            "severity": "p1",
            "env": "prod",
            "signature": "y",
            "muted": 3,
        },
    ]
    input_path = tmp_path / "muted.json"
    _write_json(input_path, rows)
    _, summary, windows, _ = _run_pipeline(tmp_path / "run", input_path=input_path)
    assert summary["muted_excluded_count"] == 1
    assert sum(w["alert_count"] for env_windows in windows.values() for w in env_windows) == 1


def test_pipeline_does_not_reference_test_artifacts():
    code = WORKFLOW_PATH.read_text(encoding="utf-8")
    for token in ("/tests", "fixtures/alt_events.json", "expected_summary.json"):
        assert token not in code

