#!/bin/bash
# Preparation (not an evaluation step) — start the Ray head and join all workers in config.json.
#   ./setup_ray.sh             start the head if needed, join missing workers
#   ./setup_ray.sh --restart   stop Ray everywhere, then start from scratch
#   ./setup_ray.sh --stop      stop Ray everywhere
source "$(dirname "${BASH_SOURCE[0]}")/../scripts/lib.sh"
use_prepare_dir
run_step setup_ray "PREPARE  Ray cluster setup" setup_ray.py "$@"
