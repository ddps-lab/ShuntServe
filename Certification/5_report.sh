#!/bin/bash
# Step 5/5 — Compare the two measurements: throughput ratio and improvement (exit 2 = result not valid).
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"
use_latest_run
run_step 5_report "STEP 5/5  Report" report.py --run-dir "$RUN_DIR"
