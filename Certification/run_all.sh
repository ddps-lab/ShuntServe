#!/bin/bash
# Run the whole evaluation: steps 1 to 5 in order, stopping at the first failure.
# Run on the Ray head node. Each step can also be run on its own (./1_check_environment.sh, ...).
# Preparation (Ray cluster, warmup) is done beforehand with the scripts in prepare/.

CERT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for step in 1_check_environment 2_optimize 3_benchmark_vllm 4_benchmark_shuntserve 5_report; do
    "$CERT_DIR/$step.sh"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        [ "$step" != "5_report" ] && echo && echo "Stopped at $step.sh. Fix the problem and re-run that step, then continue with the next ones."
        exit "$rc"
    fi
done
