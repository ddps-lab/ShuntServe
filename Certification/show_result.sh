#!/bin/bash
# Show the result of a recorded run in the same format as 5_report.sh.
# Not part of the evaluation procedure. Nothing is written to the run directory.
#
#   ./show_result.sh                 the most recent run in records/
#   ./show_result.sh <name | path>   a specific run (a folder name in records/, or any run directory)
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"

RECORDS_DIR="$CERT_DIR/records"
target="${1:-}"
if [ -z "$target" ]; then
    target="$(ls -1d "$RECORDS_DIR"/*/ 2>/dev/null | sort | tail -1)"
    if [ -z "$target" ]; then
        echo "No runs in $RECORDS_DIR"
        exit 2
    fi
elif [ ! -d "$target" ]; then
    target="$RECORDS_DIR/$target"
fi
if [ ! -d "$target" ]; then
    echo "Run directory not found: $target"
    exit 2
fi
RUN_DIR="$(cd "$target" && pwd -P)"

title="STEP 5/5  Report"
step_header "$title"
"$PY" -u "$CERT_DIR/scripts/report.py" --run-dir "$RUN_DIR" --print-only 2>&1
rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ]; then
    echo
    echo "FAILED: $title (exit code $rc). Log: $RUN_DIR/logs/5_report.log"
fi
exit "$rc"
