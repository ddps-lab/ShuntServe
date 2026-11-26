#!/bin/bash
# Step 3/5 — Measure the baseline: vLLM, one homogeneous pipeline per GPU type.
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"
use_latest_run
run_step 3_benchmark_vllm "STEP 3/5  Benchmark — baseline (vLLM, homogeneous pipelines)" benchmark.py --run-dir "$RUN_DIR" --system vllm
