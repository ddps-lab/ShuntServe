"""
Step 2 — Model placement for the evaluation cluster (CPU only, no GPU needed).

  shuntserve : ShuntServe optimizer (DP + beam search). Pipelines are extracted
               greedily until the cluster is exhausted; a pipeline may mix GPU types.
  vllm       : Baseline. One pipeline per GPU type (homogeneous), layers split evenly.

Both use the same analytical estimator and the same calibrated hardware specs.
Ported from ArtifactEvaluation/ModelPlacement/optimizer/llama3-70b/{shuntserve,vllm}.py;
the cluster comes from Certification/config.json instead of being hard-coded.

Output: <run-dir>/placement/predicted_{shuntserve,vllm}.json
"""
import argparse
import contextlib
import io
import json
import logging
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (REPO_ROOT, banner, cluster_description, load_config, node_counts,
                    placement_path, plain_instance_type, resolve_run_dir)

sys.path.insert(0, os.path.join(REPO_ROOT, "ModelPlacement"))

import torch  # noqa: E402
from transformers import AutoConfig  # noqa: E402
from cluster_pool import DEFAULT_ONDEMAND_PRICES, ClusterPool  # noqa: E402
from estimator_utils import get_global_batch_size, get_throughput  # noqa: E402
from hardware_specs import INSTANCE_SPEC  # noqa: E402
from shuntserve_optimizer import run_test_case  # noqa: E402

# shuntserve_optimizer configures the root logger on import; keep library chatter off the screen.
logging.getLogger().handlers.clear()
logging.getLogger().setLevel(logging.WARNING)

# Same settings as the paper's optimizer runs.
TOP_K = 3                 # beam width
MAX_STAGES = 13
MAX_MODEL_LEN = 8192
GPU_MEM_UTILIZATION = 0.85
PRICES = {                # $/hr, used by the throughput-per-cost objective
    "(spot)g5.12xlarge": 2.2915,
    "(spot)g6.12xlarge": 1.9445,
    "(spot)g6e.xlarge": 0.7040,
}


class ProgressLine(logging.Handler):
    """Shows the optimizer's per-layer progress log as one line that updates in place."""
    PATTERN = re.compile(r"Layer\s+(\d+)/(\d+) done.*?total=\s*([\d.]+)s.*?candidates=\s*(\d+)")

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.active = False

    def emit(self, record):
        m = self.PATTERN.search(record.getMessage())
        if not m:
            return
        layer, num_layers, elapsed, candidates = m.groups()
        sys.stdout.write(f"\r  Searching: layer {int(layer):>3}/{num_layers}   "
                         f"candidates {int(candidates):>6}   elapsed {float(elapsed):>6.1f}s")
        sys.stdout.flush()
        self.active = True

    def finish(self):
        if self.active:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.active = False


def search_pipeline(config, nodes, prices, progress, optimization_mode):
    """One optimizer call over the given nodes. Returns (ranked pipelines, optimization time)."""
    with contextlib.redirect_stdout(io.StringIO()):   # ClusterPool prints unrelated price warnings
        cluster_pool = ClusterPool(available_spot_nodes=nodes, spot_prices=prices)
    results, _, opt_time = run_test_case(
        config, budget=9999, latency_slo=99999999,
        cluster_pool=cluster_pool, max_stages=MAX_STAGES, top_k=TOP_K,
        optimization_mode=optimization_mode
    )
    progress.finish()
    return results, opt_time


def describe_nodes(counter):
    """'g6.12xlarge×2, g6e.xlarge×4' from {instance name: count}."""
    return ", ".join(f"{plain_instance_type(k)}×{v}" for k, v in counter.items() if v > 0)


def price_of(instance_type):
    """$/hr for the throughput-per-cost objective: the paper's spot price, else the on-demand price."""
    return PRICES.get(estimator_name(instance_type), DEFAULT_ONDEMAND_PRICES[instance_type])


def estimator_name(instance_type):
    """Instance key carrying the calibrated hardware spec in hardware_specs.py."""
    return f"(spot){instance_type}"


def build_model_config(model_name, workload):
    model_config = AutoConfig.from_pretrained(model_name)
    return {
        "expected_input_len": workload["expected_input_len"],
        "expected_output_len": workload["expected_output_len"],
        "hidden_size": model_config.hidden_size,
        "num_layers": model_config.num_hidden_layers,
        "num_attention_heads": model_config.num_attention_heads,
        "num_key_value_heads": getattr(model_config, "num_key_value_heads", model_config.num_attention_heads),
        "intermediate_size": model_config.intermediate_size,
        "vocab_size": model_config.vocab_size,
        "max_position_embeddings": model_config.max_position_embeddings,
        "dtype": torch.float16,
        "max_model_len": MAX_MODEL_LEN,
        "gpu_mem_utilization": GPU_MEM_UTILIZATION,
    }


def estimator_args(config):
    """Arguments shared by the estimator functions (model shape, workload, memory budget)."""
    return dict(
        avg_input_len=config["expected_input_len"],
        avg_output_len=config["expected_output_len"],
        max_model_len=MAX_MODEL_LEN,
        hidden_dim=config["hidden_size"],
        num_attention_head=config["num_attention_heads"],
        num_kv_cache_head=config["num_key_value_heads"],
        total_num_layers=config["num_layers"],
        vocab_size=config["vocab_size"],
        intermediate_dim=config["intermediate_size"],
        gpu_mem_utilization=GPU_MEM_UTILIZATION,
        dtype=torch.float16,
    )


def memory_batch_size(config, stages):
    """
    Max batch size and KV cache blocks that fit in GPU memory for a pipeline,
    given its stages as [(instance name, layer count), ...].
    Used for both configurations so the same hardware always yields the same numbers.
    """
    node_layer_comb = [(inst, "us-east-1a", int(layers)) for inst, layers in stages]
    max_batch_size, num_blocks = get_global_batch_size(node_layer_comb=node_layer_comb, **estimator_args(config))
    return int(max_batch_size), int(num_blocks)


def save_placement(run_dir, system, model_name, workload, counts, pipelines, extra=None):
    output = {
        "model": model_name,
        "workload": {"input_len": workload["expected_input_len"], "output_len": workload["expected_output_len"]},
        "cluster": counts,
        "pipelines": pipelines,
        "total_throughput_rps": sum(p["predicted_throughput_rps"] for p in pipelines),
    }
    output.update(extra or {})
    path = placement_path(run_dir, system)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {path}")


# ─── ShuntServe (heterogeneous) ──────────────────────────────────────

def optimize_shuntserve(run_dir, cfg):
    progress = ProgressLine()
    optimizer_logger = logging.getLogger("shuntserve_optimizer")
    optimizer_logger.handlers = [progress]
    optimizer_logger.setLevel(logging.INFO)
    optimizer_logger.propagate = False

    model_name = cfg["model_name"]
    counts = node_counts(cfg)
    config = build_model_config(model_name, cfg["workload"])
    prices = {estimator_name(t): price_of(t) for t in counts}

    banner("ShuntServe Optimizer — Iterative Pipeline Search (heterogeneous GPU pipelines)")
    print(f"Model: {model_name}")
    print(f"Cluster: {cluster_description(cfg)}")
    print(f"Beam width (top_k): {TOP_K}")

    remaining_nodes = {estimator_name(t): n for t, n in counts.items()}
    all_pipelines = []
    total_opt_time = 0.0
    iteration = 0

    while True:
        iteration += 1
        if all(v == 0 for v in remaining_nodes.values()):
            print("\nAll nodes are assigned.")
            break

        print(f"\nIteration {iteration} — remaining nodes: {describe_nodes(remaining_nodes)}")
        results, opt_time = search_pipeline(config, remaining_nodes, prices, progress, "soft_slo")
        total_opt_time += opt_time

        # Treat infeasible results (throughput <= 0) as no results
        if results and results[0].throughput <= 0:
            results = []

        if not results:
            # ─── Residual resource merging ────────────────────────────
            # Remaining nodes can't form an independent pipeline.
            # Merge them into the last pipeline and re-optimize for throughput.
            if all_pipelines and any(v > 0 for v in remaining_nodes.values()):
                print(f"  The remaining nodes cannot hold the model on their own; "
                      f"merging them into Pipeline {iteration - 1}.")
                last_pipeline = all_pipelines[-1]
                merged_nodes = dict(remaining_nodes)
                for inst, count in Counter(last_pipeline.stages).items():
                    merged_nodes[inst] = merged_nodes.get(inst, 0) + count

                merged_results, merged_opt_time = search_pipeline(
                    config, merged_nodes, prices, progress, "only_throughput")
                total_opt_time += merged_opt_time

                if merged_results:
                    all_pipelines[-1] = merged_results[0]
                    for k in remaining_nodes:
                        remaining_nodes[k] = 0
                    print(f"  → Pipeline {iteration - 1} (re-optimized): "
                          f"{describe_nodes(Counter(merged_results[0].stages))}")
                else:
                    print("  Re-optimization with the merged nodes also failed.")
            else:
                print("  No feasible pipeline found with the remaining nodes.")
            break

        # Keep the best pipeline and subtract the nodes it uses
        best = results[0]
        all_pipelines.append(best)
        print(f"  → Pipeline {iteration}: {describe_nodes(Counter(best.stages))}")
        for inst, count in Counter(best.stages).items():
            if inst in remaining_nodes:
                remaining_nodes[inst] -= count

    if not all_pipelines:
        sys.exit("ShuntServe optimizer found no feasible pipeline for this cluster.")

    # ─── Final result ────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"FINAL RESULT (ShuntServe): {len(all_pipelines)} pipeline(s)")
    print(f"{'=' * 70}")
    output_pipelines = []
    for i, pipeline in enumerate(all_pipelines, 1):
        tp_list = [INSTANCE_SPEC[inst]["gpu_count"] for inst in pipeline.stages]
        # The optimizer derives its batch size back from the block count, which rounds down
        # twice and can land one below what memory allows. Compute it from memory directly,
        # exactly as for the baseline; the KV cache size (num_blocks) is unchanged.
        max_batch_size, num_blocks = memory_batch_size(config, zip(pipeline.stages, pipeline.layer_per_stage))
        assert num_blocks == pipeline.num_blocks, (
            f"Pipeline {i}: KV cache blocks differ (optimizer {pipeline.num_blocks}, estimator {num_blocks})")
        print(f"\nPipeline {i}:")
        print(f"  Instances: {describe_nodes(Counter(pipeline.stages))}")
        print(f"  Stages (in order): {' → '.join(plain_instance_type(inst) for inst in pipeline.stages)}")
        print(f"  TP strategy: {tp_list}")
        print(f"  Layer partition: {[int(l) for l in pipeline.layer_per_stage]}")
        print(f"  Max batch size: {max_batch_size}")
        print(f"  Num GPU blocks: {num_blocks}")
        output_pipelines.append({
            "label": f"SS-P{i}",
            "system": "ShuntServe",
            "stages": [[inst, int(layers)] for inst, layers in zip(pipeline.stages, pipeline.layer_per_stage)],
            "parallel_strategy": tp_list,
            "pp_layer_partition": ",".join(str(int(l)) for l in pipeline.layer_per_stage),
            "gpu_memory_utilization": GPU_MEM_UTILIZATION,
            "predicted_throughput_rps": pipeline.throughput,   # used as the request routing weight
            "predicted_total_latency_ms": pipeline.latency_per_global_batch,
            "max_batch_size": max_batch_size,
            "num_blocks": num_blocks,
        })

    print(f"\nOptimization time: {total_opt_time:.1f}s")
    unused = {k: v for k, v in remaining_nodes.items() if v > 0}
    if unused:
        print(f"Unused nodes: {describe_nodes(unused)}")

    save_placement(run_dir, "shuntserve", model_name, cfg["workload"], counts, output_pipelines,
                   extra={"top_k": TOP_K, "optimization_time_s": total_opt_time, "unused_nodes": unused})


# ─── vLLM (homogeneous baseline) ─────────────────────────────────────

def even_partition(num_layers, num_stages):
    """Even layer partition (vLLM default)."""
    base, remainder = divmod(num_layers, num_stages)
    return [base + (1 if i < remainder else 0) for i in range(num_stages)]


def optimize_vllm(run_dir, cfg):
    model_name = cfg["model_name"]
    counts = node_counts(cfg)
    config = build_model_config(model_name, cfg["workload"])

    banner("vLLM Baseline — one homogeneous pipeline per GPU type (even layer partition)")
    print(f"Model: {model_name}")
    print(f"Cluster: {cluster_description(cfg)}")

    output_pipelines = []
    for i, (instance_type, num_instances) in enumerate(counts.items(), 1):
        inst = estimator_name(instance_type)
        gpu_type = INSTANCE_SPEC[inst]["gpu_type"]
        tp_size = INSTANCE_SPEC[inst]["gpu_count"]
        layers_per_stage = even_partition(config["num_layers"], num_instances)
        node_layer_comb = [(inst, "us-east-1a", l) for l in layers_per_stage]

        throughput, batch_latency, _ = get_throughput(node_layer_comb=node_layer_comb, **estimator_args(config))
        max_batch_size, num_blocks = memory_batch_size(config, [(inst, l) for l in layers_per_stage])

        print(f"\n{'─' * 60}")
        print(f"Pipeline {i}: {instance_type} × {num_instances}")
        print(f"  GPU: {gpu_type} (TP={tp_size}), PP={num_instances}")
        print(f"  Layer partition: {layers_per_stage}")
        print(f"  Max batch size: {max_batch_size}")
        print(f"  Num GPU blocks: {num_blocks}")

        if throughput <= 0 or max_batch_size < 1:
            sys.exit(f"{instance_type} × {num_instances} cannot hold the model as a homogeneous pipeline; "
                     "the baseline is undefined for this cluster.")

        output_pipelines.append({
            "label": f"vL-P{i}",
            "system": "vLLM",
            "stages": [[inst, l] for l in layers_per_stage],
            "parallel_strategy": [tp_size] * num_instances,
            "pp_layer_partition": ",".join(str(l) for l in layers_per_stage),
            "gpu_memory_utilization": GPU_MEM_UTILIZATION,
            "predicted_throughput_rps": throughput,   # used as the request routing weight
            "predicted_total_latency_ms": batch_latency,
            "max_batch_size": max_batch_size,
            "num_blocks": num_blocks,
        })

    save_placement(run_dir, "vllm", model_name, cfg["workload"], counts, output_pipelines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", default=None, help="Run directory (default: results/latest)")
    parser.add_argument("--system", choices=["shuntserve", "vllm", "all"], default="all")
    args = parser.parse_args()

    run_dir = resolve_run_dir(args.run_dir)
    cfg = load_config()
    start = time.time()
    if args.system in ("vllm", "all"):
        optimize_vllm(run_dir, cfg)
    if args.system in ("shuntserve", "all"):
        optimize_shuntserve(run_dir, cfg)
    print(f"\nPlacement step finished in {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()
