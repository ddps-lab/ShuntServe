#!/bin/bash
# Step 1/5 — Show and verify the evaluation environment. Starts a new run directory.
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"
start_new_run
run_step 1_check_environment "STEP 1/5  Environment check" check_environment.py
