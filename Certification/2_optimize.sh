#!/bin/bash
# Step 2/5 — Compute the model placement of both configurations.
# Needs no GPU, no cluster and no node IPs: only the node counts in config.json are used.
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"
use_latest_or_new_run
run_step 2_optimize "STEP 2/5  Model placement (ShuntServe optimizer and vLLM baseline)" optimize.py --run-dir "$RUN_DIR"
