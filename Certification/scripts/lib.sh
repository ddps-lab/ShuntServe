# Shared by the step scripts and the prepare/ scripts (sourced, not executed).
#
# Evaluation steps write into one run directory, results/<start time>/.
# Step 1 starts a new run directory; later steps continue in the most recent one.
# Preparation scripts (prepare/) write into results/prepare/ and never touch a run directory.

set -uo pipefail

CERT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESULTS_DIR="$CERT_DIR/results"
CONFIG_FILE="${CERT_CONFIG:-$CERT_DIR/config.json}"
PY="${PYTHON:-python3}"

# A new run directory keeps a copy of the config it was started with, so its report
# can be produced again later with the same evaluation conditions.
start_new_run() {
    RUN_DIR="$RESULTS_DIR/$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$RUN_DIR/logs"
    cp "$CONFIG_FILE" "$RUN_DIR/config.json"
    ln -sfn "$(basename "$RUN_DIR")" "$RESULTS_DIR/latest"
}

use_latest_run() {
    if [ ! -d "$RESULTS_DIR/latest" ]; then
        echo "No run directory yet. Start with ./1_check_environment.sh (or ./run_all.sh)."
        exit 2
    fi
    RUN_DIR="$(cd "$RESULTS_DIR/latest" && pwd -P)"
    mkdir -p "$RUN_DIR/logs"
}

# For steps that need nothing from earlier steps: continue the latest run, or start one.
use_latest_or_new_run() {
    if [ -d "$RESULTS_DIR/latest" ]; then use_latest_run; else start_new_run; fi
}

use_prepare_dir() {
    RUN_DIR="$RESULTS_DIR/prepare"
    mkdir -p "$RUN_DIR/logs"
}

step_header() {
    echo
    echo "################################################################################"
    echo "# $1"
    echo "# $(date '+%Y-%m-%d %H:%M:%S')   run directory: $RUN_DIR"
    echo "################################################################################"
}

# run_step <log name> <title> <python script> [args...]
# Shows the output on screen and saves it to <run dir>/logs/<log name>.log.
run_step() {
    local name="$1" title="$2" script="$3"; shift 3
    local log="$RUN_DIR/logs/${name}.log"
    step_header "$title"
    "$PY" -u "$CERT_DIR/scripts/$script" "$@" 2>&1 | tee "$log"
    local rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        echo
        echo "FAILED: $title (exit code $rc). Log: $log"
    fi
    return "$rc"
}
