"""
Step 1 — Show and verify the evaluation environment (run on the Ray head node).

Prints the cluster composition (instance types, GPUs, node counts) and the model
with its weight size. Everything else is verified silently and reported only
when something is wrong: node IPs, SSH access, instance type and GPU count of
every node, installed software, model weights in S3, Hugging Face token, Ray,
and the dataset. Nothing is started or modified.
"""
import argparse
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import REPO_ROOT, REMOTE_PROCESS_PATTERNS, banner, load_config, ssh_run, validate_nodes

sys.path.insert(0, os.path.join(REPO_ROOT, "GlobalServer"))
sys.path.insert(0, os.path.join(REPO_ROOT, "ModelPlacement"))
from command import PROJECT_PATH, PYTHON, RAY  # paths GlobalServer uses on every node  # noqa: E402
from hardware_specs import INSTANCE_SPEC  # noqa: E402

WEIGHT_DTYPE = "BF16"
WEIGHT_BYTES_PER_PARAM = 2


# ─── What is shown ───────────────────────────────────────────────────

def model_parameter_count(model_name):
    """Parameter count of a decoder-only transformer, computed from its Hugging Face config."""
    from transformers import AutoConfig
    c = AutoConfig.from_pretrained(model_name)
    hidden, inter, layers, vocab = c.hidden_size, c.intermediate_size, c.num_hidden_layers, c.vocab_size
    head_dim = getattr(c, "head_dim", None) or hidden // c.num_attention_heads
    q_dim = c.num_attention_heads * head_dim
    kv_dim = getattr(c, "num_key_value_heads", c.num_attention_heads) * head_dim
    attention = hidden * q_dim + 2 * hidden * kv_dim + q_dim * hidden      # q, k, v, o projections
    mlp = 3 * hidden * inter                                               # gate, up, down projections
    norms = 2 * hidden
    embeddings = vocab * hidden * (1 if getattr(c, "tie_word_embeddings", False) else 2)
    return layers * (attention + mlp + norms) + embeddings + hidden


def print_environment(cfg, params):
    banner("Evaluation environment")
    print(f"Model   : {cfg['model_name']}")
    if params is not None:
        size_gb = params * WEIGHT_BYTES_PER_PARAM / 1e9
        print(f"Weights : {size_gb:.1f} GB ({params / 1e9:.1f}B parameters, {WEIGHT_DTYPE})")

    rows = []
    for itype, ips in cfg["nodes"].items():
        spec = INSTANCE_SPEC[itype]
        rows.append((itype, spec["gpu_type"], spec["gpu_count"], len(ips), spec["gpu_count"] * len(ips)))
    total_nodes = sum(r[3] for r in rows)
    total_gpus = sum(r[4] for r in rows)
    gpu_types = len({r[1] for r in rows})

    print(f"Cluster : {total_nodes} nodes, {total_gpus} GPUs, {gpu_types} GPU types\n")
    print(f"  {'Instance type':<16}{'GPU':<8}{'GPUs per node':>14}{'Nodes':>8}{'GPUs':>7}")
    for itype, gpu, per_node, nodes, gpus in rows:
        print(f"  {itype:<16}{gpu:<8}{per_node:>14}{nodes:>8}{gpus:>7}")
    print(f"  {'Total':<16}{'':<8}{'':>14}{total_nodes:>8}{total_gpus:>7}")


# ─── What is verified ────────────────────────────────────────────────

def node_check_script(cfg):
    """Bash script run on each node; prints key=value lines."""
    s3_key = f"raw/{cfg['model_name']}/config.json"
    pattern = "|".join(REMOTE_PROCESS_PATTERNS)
    return f"""
TOKEN=$(curl -s -m 3 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
echo "itype=$(curl -s -m 3 -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-type)"
echo "gpus=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l)"
echo "repo=$([ -f {PROJECT_PATH}/TensorStore/raw_s3_tensor_store_server.py ] && echo ok || echo missing)"
echo "python=$([ -x {PYTHON} ] && echo ok || echo missing)"
echo "ray=$([ -x {RAY} ] && echo ok || echo missing)"
echo "hf_token=$([ -n "$HF_TOKEN" ] && echo ok || echo missing)"
echo "procs=$(pgrep -fc '{pattern}')"
echo "s3=$({PYTHON} -c "import boto3; boto3.client('s3').head_object(Bucket='{cfg['s3_bucket']}', Key='{s3_key}'); print('ok')" 2>/dev/null || echo fail)"
"""


def check_node(args):
    expected_type, ip, script = args
    rc, out, err = ssh_run(ip, script, timeout=90)
    info = {k: v.strip() for k, v in (line.split("=", 1) for line in out.splitlines() if "=" in line)}
    return expected_type, ip, rc, info, err.strip()


def check_head(cfg, config_ok):
    """Checks on this machine. Returns (errors, warnings)."""
    errors, warnings = [], []

    if not os.path.exists(os.path.join(REPO_ROOT, cfg["workload"]["dataset"])):
        errors.append(f"dataset not found: {cfg['workload']['dataset']}")

    gpu_types = {INSTANCE_SPEC[t]["gpu_type"] for t in cfg["nodes"]}
    if len(gpu_types) < 3:
        errors.append(f"the cluster has {len(gpu_types)} GPU type(s); the evaluation requires at least 3")

    if not (os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
            or os.path.exists(os.path.expanduser("~/.cache/huggingface/token"))):
        errors.append("head: Hugging Face token not set (export HF_TOKEN)")

    ray_bin = shutil.which("ray") or (RAY if os.path.exists(RAY) else None)
    if ray_bin is None:
        errors.append("head: ray executable not found")
        return errors, warnings
    try:
        running = subprocess.run([ray_bin, "status"], capture_output=True, text=True, timeout=60).returncode == 0
    except subprocess.TimeoutExpired:
        running = False
    if not running:
        errors.append("head: Ray is not running (run prepare/setup_ray.sh)")
    elif config_ok:
        try:
            from setup_ray import cluster_nodes
            joined = cluster_nodes()
            missing = [ip for ips in cfg["nodes"].values() for ip in ips if ip not in joined]
            if missing:
                warnings.append(f"not in the Ray cluster yet: {', '.join(missing)} "
                                "(run prepare/setup_ray.sh; otherwise they are joined when the benchmark starts)")
        except Exception as e:
            warnings.append(f"could not query the Ray cluster: {e}")
    return errors, warnings


def check_workers(cfg):
    """Checks on every worker over SSH. Returns (errors, warnings)."""
    errors, warnings = [], []
    script = node_check_script(cfg)
    tasks = [(itype, ip, script) for itype, ips in cfg["nodes"].items() for ip in ips]
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        results = list(ex.map(check_node, tasks))
    for expected_type, ip, rc, info, err in results:
        name = f"{ip} ({expected_type})"
        if rc != 0 or not info:
            errors.append(f"{name}: SSH failed — {err or 'no output'}")
            continue
        if info.get("itype") != expected_type:
            errors.append(f"{name}: instance type is {info.get('itype') or 'unknown'}")
        expected_gpus = INSTANCE_SPEC[expected_type]["gpu_count"]
        if info.get("gpus") != str(expected_gpus):
            errors.append(f"{name}: {info.get('gpus')} GPU(s) visible, expected {expected_gpus}")
        for key, problem in (("repo", f"{PROJECT_PATH} missing"), ("python", f"{PYTHON} missing"),
                             ("ray", f"{RAY} missing"), ("hf_token", "HF_TOKEN not set"),
                             ("s3", f"cannot read s3://{cfg['s3_bucket']}/raw/{cfg['model_name']}/")):
            if info.get(key) != "ok":
                errors.append(f"{name}: {problem}")
        if info.get("procs", "0") not in ("0", ""):
            warnings.append(f"{name}: {info['procs']} leftover serving process(es) "
                            "(stopped automatically when a benchmark starts)")
    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args()
    cfg = load_config()

    errors, warnings = [], []
    try:
        params = model_parameter_count(cfg["model_name"])
    except Exception as e:
        params = None
        errors.append(f"cannot load the model configuration of {cfg['model_name']} from Hugging Face: {e}")

    print_environment(cfg, params)

    config_problems = [f"config.json: {p}" for p in validate_nodes(cfg)]
    errors += config_problems
    for found_errors, found_warnings in ([check_head(cfg, not config_problems)]
                                         + ([check_workers(cfg)] if not config_problems else [])):
        errors += found_errors
        warnings += found_warnings

    if warnings:
        print("\nWarnings")
        for w in warnings:
            print(f"  - {w}")
    if errors:
        print("\nProblems")
        for e in errors:
            print(f"  - {e}")
        print(f"\nENVIRONMENT CHECK FAILED: {len(errors)} problem(s).")
        sys.exit(1)
    print("\nEnvironment check passed.")


if __name__ == "__main__":
    main()
