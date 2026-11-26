#!/bin/bash
# Preparation (not an evaluation step) — launch each configuration once and serve a short
# trace window, so every node has loaded the software stack and the model once.
# Results go to results/prepare/ and are never used by the evaluation report.
source "$(dirname "${BASH_SOURCE[0]}")/../scripts/lib.sh"
use_prepare_dir
run_step warmup_optimize "PREPARE  Model placement for warmup" optimize.py --run-dir "$RUN_DIR" || exit $?
run_step warmup_vllm "PREPARE  Warmup — baseline (vLLM, homogeneous pipelines)" benchmark.py --run-dir "$RUN_DIR" --system vllm --warmup || exit $?
run_step warmup_shuntserve "PREPARE  Warmup — ShuntServe (heterogeneous pipelines)" benchmark.py --run-dir "$RUN_DIR" --system shuntserve --warmup
