#!/bin/bash
# Copy a run directory from the head node into records/ (run on your own machine).
# Not part of the evaluation procedure.
#
#   ./records/fetch.sh <head address> [run name]
#
# The run name defaults to the head's latest run (results/latest). The run is stored
# as records/<run name>/ with the same layout as on the head; an existing folder of
# the same name is never overwritten.
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <head address> [run name]"
    exit 2
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="$1"
case "$HOST" in *@*) ;; *) HOST="ubuntu@$HOST" ;; esac
RUN="${2:-}"
SSH=(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes)
REMOTE_RESULTS='~/ShuntServe/Certification/results'

if [ -z "$RUN" ]; then
    RUN="$("${SSH[@]}" "$HOST" "basename \"\$(readlink -f $REMOTE_RESULTS/latest)\"")"
fi
if [ -e "$HERE/$RUN" ]; then
    echo "records/$RUN already exists; not overwriting."
    exit 1
fi

echo "Copying $HOST:$REMOTE_RESULTS/$RUN → records/$RUN"
"${SSH[@]}" "$HOST" "tar -czf - -C $REMOTE_RESULTS '$RUN'" | tar -xzf - -C "$HERE"

echo "Done: $(find "$HERE/$RUN" -type f | wc -l | tr -d ' ') files, $(du -sh "$HERE/$RUN" | cut -f1)"
for f in config.json placement/predicted_vllm.json placement/predicted_shuntserve.json \
         measured/offline_vllm.json measured/offline_shuntserve.json report.md; do
    [ -f "$HERE/$RUN/$f" ] || echo "  warning: $f is missing"
done
