"""
Preparation — start the Ray cluster used by the evaluation (run on the head node).

First confirms that every worker listed in config.json is up and accepts an
SSH login (key authentication, no host fingerprint question). Then starts the
Ray head on this machine if it is not running, joins every worker over SSH and
waits until all of them are alive with the expected number of GPUs. Safe to run
again: nodes already in the cluster are left alone.

  setup_ray.py            start the head if needed and join missing workers
  setup_ray.py --restart  stop Ray everywhere first, then start from scratch
  setup_ray.py --stop     stop Ray on the workers and the head
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import REPO_ROOT, all_node_ips, banner, load_config, require_ssh, ssh_run, validate_nodes

sys.path.insert(0, os.path.join(REPO_ROOT, "GlobalServer"))
sys.path.insert(0, os.path.join(REPO_ROOT, "ModelPlacement"))
from command import RAY, get_ray_start_worker_command  # noqa: E402
from hardware_specs import INSTANCE_SPEC  # noqa: E402

RAY_PORT = 6379          # port GlobalServer connects to
JOIN_TIMEOUT_S = 180


def head_ray_bin():
    return shutil.which("ray") or RAY


def head_running():
    return subprocess.run([head_ray_bin(), "status"], capture_output=True, text=True).returncode == 0


def run_on_workers(ips, script, timeout=120):
    with ThreadPoolExecutor(max_workers=max(1, len(ips))) as ex:
        return dict(zip(ips, ex.map(lambda ip: ssh_run(ip, script, timeout=timeout), ips)))


def cluster_nodes():
    """{ip: gpu_count} for the nodes currently alive in the Ray cluster."""
    import ray
    if not ray.is_initialized():
        ray.init(address="auto", logging_level="ERROR", ignore_reinit_error=True)
    return {n.get("NodeManagerAddress"): int(n.get("Resources", {}).get("GPU", 0))
            for n in ray.nodes() if n.get("Alive")}


def stop_all(ips):
    print("Stopping Ray on the workers...")
    for ip, (rc, _, err) in run_on_workers(ips, f"{RAY} stop --force").items():
        print(f"  {ip}: {'stopped' if rc == 0 else 'failed — ' + err.strip()}")
    print("Stopping Ray on the head...")
    subprocess.run([head_ray_bin(), "stop", "--force"], capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--restart", action="store_true", help="Stop Ray on every node first")
    group.add_argument("--stop", action="store_true", help="Stop Ray on every node and exit")
    args = parser.parse_args()

    cfg = load_config()
    problems = validate_nodes(cfg)
    if problems:
        sys.exit("config.json: " + "; ".join(problems))
    workers = all_node_ips(cfg)
    expected_gpus = {ip: INSTANCE_SPEC[itype]["gpu_count"] for itype, ips in cfg["nodes"].items() for ip in ips}
    head_ip = socket.gethostbyname(socket.gethostname())   # same lookup GlobalServer uses
    head_address = f"{head_ip}:{RAY_PORT}"

    banner("Ray cluster setup")
    print(f"Head: {head_address}   Workers: {len(workers)}")

    print("\nChecking SSH access to the workers...")
    require_ssh(workers)
    print(f"All {len(workers)} workers are up and reachable over SSH.")

    if args.stop or args.restart:
        stop_all(workers)
        if args.stop:
            return

    # ─── Head ────────────────────────────────────────────────────────
    if head_running():
        print("\nRay head is already running.")
    else:
        print("\nStarting Ray head...")
        r = subprocess.run([head_ray_bin(), "start", "--head", f"--port={RAY_PORT}", "--disable-usage-stats"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"Failed to start Ray head:\n{r.stdout}\n{r.stderr}")
        print("Ray head started.")

    # ─── Workers ─────────────────────────────────────────────────────
    joined = cluster_nodes()
    to_join = [ip for ip in workers if ip not in joined]
    if to_join:
        print(f"\nJoining {len(to_join)} worker(s) to the cluster...")
        # A worker may still run a Ray daemon from an earlier cluster; stop it before joining.
        join_script = f"{RAY} stop --force >/dev/null 2>&1; {get_ray_start_worker_command(head_address)}"
        failed = []
        for ip, (rc, out, err) in run_on_workers(to_join, join_script).items():
            if rc != 0:
                failed.append(ip)
                print(f"  {ip}: FAILED — {(err or out).strip().splitlines()[-1] if (err or out).strip() else 'no output'}")
        if failed:
            sys.exit(f"Could not start Ray on: {', '.join(failed)}")
    else:
        print("\nAll workers are already in the cluster.")

    # ─── Verify ──────────────────────────────────────────────────────
    deadline = time.time() + JOIN_TIMEOUT_S
    while True:
        joined = cluster_nodes()
        ready = [ip for ip in workers if joined.get(ip) == expected_gpus[ip]]
        if len(ready) == len(workers) or time.time() > deadline:
            break
        time.sleep(2)

    print("\nCluster status")
    ok = True
    for itype, ips in cfg["nodes"].items():
        for ip in ips:
            gpus = joined.get(ip)
            good = gpus == expected_gpus[ip]
            ok &= good
            state = "not in cluster" if gpus is None else f"{gpus} GPU(s)"
            print(f"  [{'OK' if good else 'FAIL':^4}] {ip:<16} {itype:<12} {state}"
                  + ("" if good else f" (expected {expected_gpus[ip]})"))
    if not ok:
        sys.exit(f"\nRay cluster is NOT ready after {JOIN_TIMEOUT_S}s.")
    print(f"\nRay cluster is ready: head + {len(workers)} workers, {sum(expected_gpus.values())} GPUs.")


if __name__ == "__main__":
    main()
