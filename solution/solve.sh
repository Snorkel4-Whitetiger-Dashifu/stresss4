#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cp "${SCRIPT_DIR}/export_report_fixed.py" /app/workflow/export_report.py
python3 /app/workflow/export_report.py --output-dir /app/output
