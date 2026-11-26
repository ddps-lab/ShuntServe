#!/bin/bash
# Step 4/5 — Measure ShuntServe: pipelines that mix GPU types.
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"
use_latest_run
run_step 4_benchmark_shuntserve "STEP 4/5  Benchmark — ShuntServe (heterogeneous pipelines)" benchmark.py --run-dir "$RUN_DIR" --system shuntserve
