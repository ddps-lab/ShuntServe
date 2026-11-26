"""
Shared helpers for the certification evaluation scripts.

Everything a run produces (placements, logs, traces, measured results, report)
is written under  Certification/results/<run-id>/.
"""
import json
import logging
import os
import subprocess
import sys
from collections import Counter

CERT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(CERT_DIR, "results")
# CERT_CONFIG points to another config file (e.g. a single-node test); defaults to config.json.
CONFIG_PATH = os.environ.get("CERT_CONFIG") or os.path.join(CERT_DIR, "config.json")

_d = CERT_DIR
while not os.path.exists(os.path.join(_d, ".git")):
    _parent = os.path.dirname(_d)
    if _parent == _d:
        raise RuntimeError("Repository root not found (no .git directory above Certification/)")
    _d = _parent
REPO_ROOT = _d
del _d

SYSTEMS = {
    "shuntserve": "ShuntServe",   # heterogeneous-GPU pipelines (proposed)
    "vllm": "vLLM",               # homogeneous-GPU pipelines (baseline)
}

# Key authentication only, never interactive:
#   StrictHostKeyChecking=no + UserKnownHostsFile=/dev/null  skip the host fingerprint question
#   BatchMode=yes                                            fail instead of asking for a password
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
]

REMOTE_PROCESS_PATTERNS = ["s3_tensor_store_server.py", "InferenceServer/api_server.py"]


# ─── Config ──────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_run_config(run_dir):
    """The config a run was started with (copied into the run directory), else the current config."""
    saved = os.path.join(run_dir, "config.json")
    if os.path.exists(saved):
        with open(saved) as f:
            return json.load(f)
    return load_config()


def node_counts(config):
    """{"g6.12xlarge": 2, ...} from the node IP lists in config.json."""
    return {itype: len(ips) for itype, ips in config["nodes"].items()}


def all_node_ips(config):
    return [ip for ips in config["nodes"].values() for ip in ips]


def validate_nodes(config):
    """Return a list of problems with the node IP lists (empty list = OK)."""
    problems = []
    ips = all_node_ips(config)
    empty = sum(1 for ip in ips if not ip.strip())
    if empty:
        problems.append(f"{empty} node IP(s) are empty")
    filled = [ip for ip in ips if ip.strip()]
    dup = [ip for ip, n in Counter(filled).items() if n > 1]
    if dup:
        problems.append(f"duplicated node IP(s): {', '.join(dup)}")
    return problems


def cluster_description(config):
    return ", ".join(f"{itype}×{n}" for itype, n in node_counts(config).items())


# ─── Run directory layout ────────────────────────────────────────────

def resolve_run_dir(run_dir=None):
    """Use the given run directory, or results/latest when omitted."""
    if run_dir is None:
        run_dir = os.path.join(RESULTS_DIR, "latest")
        if not os.path.exists(run_dir):
            sys.exit("No run directory given and results/latest does not exist. "
                     "Use ./run_all.sh or pass --run-dir.")
    run_dir = os.path.realpath(run_dir)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def placement_path(run_dir, system):
    return os.path.join(run_dir, "placement", f"predicted_{system}.json")


def measured_path(run_dir, system, kind="offline"):
    """kind: 'offline' (the measured benchmark) or 'warmup'."""
    return os.path.join(run_dir, "measured", f"{kind}_{system}.json")


def logs_dir(run_dir, system=None):
    path = os.path.join(run_dir, "logs", system) if system else os.path.join(run_dir, "logs")
    os.makedirs(path, exist_ok=True)
    return path


# ─── Placement → node mapping ────────────────────────────────────────

def plain_instance_type(name):
    """'(spot)g6e.xlarge' → 'g6e.xlarge'."""
    return name.replace("(spot)", "")


def assign_nodes(pipelines, config):
    """
    Map every pipeline stage to a concrete node IP.

    Each stage of a placement names an instance type; IPs of that type are
    handed out in config.json order, each node used exactly once.
    Returns one list per pipeline: [(ip, num_layers), ...] in stage order.
    """
    pool = {itype: list(ips) for itype, ips in config["nodes"].items()}
    mappings = []
    for i, p in enumerate(pipelines):
        mapping = []
        for instance_type, num_layers in p["stages"]:
            itype = plain_instance_type(instance_type)
            if not pool.get(itype):
                raise ValueError(
                    f"Pipeline {i + 1} needs more {itype} nodes than config.json provides "
                    f"({len(config['nodes'].get(itype, []))})"
                )
            mapping.append((pool[itype].pop(0), int(num_layers)))
        mappings.append(mapping)
    return mappings


# ─── SSH ─────────────────────────────────────────────────────────────

def ssh_run(ip, script, timeout=60):
    """Run a bash script on a node (script is sent over stdin). Returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, ip, "bash -s"], input=script,
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"ssh to {ip} timed out after {timeout}s"


def check_ssh(ips):
    """
    Try to log in to every node. Returns {ip: None if reachable, else the reason}.
    A node that is not running, a wrong IP and a rejected key all show up here.
    """
    from concurrent.futures import ThreadPoolExecutor

    def probe(ip):
        rc, out, err = ssh_run(ip, "echo ssh-ok", timeout=30)
        if rc == 0 and "ssh-ok" in out:
            return None
        text = err.lower()
        if rc == 124 or "timed out" in text or "no route to host" in text or "unreachable" in text:
            return "no response (instance not running, wrong IP, or SSH blocked by the security group)"
        if "connection refused" in text:
            return "connection refused (instance still booting, or sshd not running)"
        if "permission denied" in text:
            return "key authentication failed (the head node's SSH key is not accepted)"
        return (err.strip().splitlines() or [f"ssh exited with code {rc}"])[-1]

    with ThreadPoolExecutor(max_workers=max(1, len(ips))) as ex:
        return dict(zip(ips, ex.map(probe, ips)))


def require_ssh(ips):
    """Exit with a per-node explanation unless every node accepts an SSH login."""
    unreachable = {ip: reason for ip, reason in check_ssh(ips).items() if reason}
    if unreachable:
        print(f"Cannot log in to {len(unreachable)} of {len(ips)} node(s) over SSH:")
        for ip, reason in unreachable.items():
            print(f"  - {ip}: {reason}")
        sys.exit(1)


def kill_remote_processes(ips, logger=None):
    """Stop leftover TensorStore / API server processes on the given nodes."""
    pattern = "|".join(REMOTE_PROCESS_PATTERNS)
    script = (
        f"pkill -f '{REMOTE_PROCESS_PATTERNS[0]}' 2>/dev/null; "
        f"pkill -f '{REMOTE_PROCESS_PATTERNS[1]}' 2>/dev/null; "
        "sleep 3; "
        f"pkill -9 -f '{REMOTE_PROCESS_PATTERNS[0]}' 2>/dev/null; "
        f"pkill -9 -f '{REMOTE_PROCESS_PATTERNS[1]}' 2>/dev/null; "
        "sleep 1; "
        f"pgrep -fc '{pattern}' || true"
    )
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, len(ips))) as ex:
        results = list(ex.map(lambda ip: (ip, ssh_run(ip, script, timeout=60)), ips))
    for ip, (rc, out, err) in results:
        remaining = out.strip().splitlines()[-1] if out.strip() else "?"
        if logger and remaining not in ("0", ""):
            logger.warning(f"  {ip}: {remaining} leftover process(es) still running ({err.strip()})")


# ─── Logging ─────────────────────────────────────────────────────────

def setup_logger(name, log_file=None):
    """Console logger, optionally mirrored to a file."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger


def banner(title):
    line = "=" * 78
    print(f"\n{line}\n{title}\n{line}", flush=True)
