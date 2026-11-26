"""
Step 3 — Offline throughput benchmark for one serving configuration.

Loads the placement produced by optimize.py, launches its pipelines on the
cluster nodes listed in config.json, submits every request of the evaluation
dataset (Certification/dataset/) at once (time_scale=0.0) and measures request throughput.

Ported from ArtifactEvaluation/ModelPlacement/offline/llama3-70b/{shuntserve,vllm}.py.
Request routing is untouched: GlobalServer picks a pipeline per request, weighted
by predicted throughput among the pipelines that have free capacity.

With --warmup the same pipelines are launched but only a short trace window is
served, so that every node has loaded the software stack and model path once
before the measured run. Warmup results are not used by the report.

Outputs (all under <run-dir>; <kind> is "offline" or "warmup"):
  measured/<kind>_<system>.json      throughput and latency metrics
  Trace/<kind>_<system>_<time>.csv   per-request trace
  logs/<system>/ or logs/warmup_<system>/   GlobalServer / Cluster / remote process logs
"""
import argparse
import asyncio
import concurrent.futures
import glob
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (REPO_ROOT, SYSTEMS, all_node_ips, assign_nodes, banner, kill_remote_processes,
                    load_config, logs_dir, measured_path, placement_path, require_ssh, resolve_run_dir,
                    setup_logger, validate_nodes)

STAGE_LAYER_COUNT_IDX = 1
HEARTBEAT_SECONDS = 30
PERCENTILES = [10, 25, 50, 75, 90, 99]


def load_placement(run_dir, system):
    path = placement_path(run_dir, system)
    if not os.path.exists(path):
        sys.exit(f"Placement file not found: {path}\nRun scripts/optimize.py first.")
    with open(path) as f:
        return json.load(f)


def print_plan(system, data, mappings):
    pipelines = data["pipelines"]
    print(f"System: {SYSTEMS[system]}   Model: {data['model']}")
    print(f"Pipelines: {len(pipelines)}")
    for i, (p, mapping) in enumerate(zip(pipelines, mappings), 1):
        print(f"\n  P{i}: TP={p['parallel_strategy']}  layers={p['pp_layer_partition']}"
              f"  max_batch_size={int(p['max_batch_size'])}")
        for stage_idx, ((instance_type, _), (ip, layers)) in enumerate(zip(p["stages"], mapping)):
            print(f"      stage {stage_idx}: {instance_type.replace('(spot)', ''):<12} {ip:<16} {layers} layers")


def tail_remote_logs(remote_log_dir, lines=15):
    """Print the tail of every remote process log (TensorStore / API server)."""
    files = sorted(glob.glob(os.path.join(remote_log_dir, "*.log")))
    if not files:
        print(f"  (no remote logs in {remote_log_dir})")
    for path in files:
        try:
            with open(path, errors="replace") as f:
                tail = f.readlines()[-lines:]
        except OSError as e:
            tail = [f"<cannot read: {e}>\n"]
        print(f"\n----- {path} (last {len(tail)} lines) -----")
        print("".join(tail).rstrip())


def latest_remote_log_line(remote_log_dir):
    files = glob.glob(os.path.join(remote_log_dir, "*.log"))
    if not files:
        return "no remote log yet"
    newest = max(files, key=os.path.getmtime)
    try:
        with open(newest, errors="replace") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
    except OSError:
        lines = []
    return f"{os.path.basename(newest)}: {lines[-1][:140] if lines else '(empty)'}"


async def run(system, run_dir, cfg, data, mappings, warmup=False):
    kind = "warmup" if warmup else "offline"
    log_dir = logs_dir(run_dir, f"warmup_{system}" if warmup else system)
    remote_log_dir = os.path.join(log_dir, "remote")
    logger = setup_logger(f"certification.{kind}.{system}", os.path.join(log_dir, "benchmark.log"))

    # GlobalServer modules (imported here so --dry-run works without a cluster)
    sys.path.insert(0, os.path.join(REPO_ROOT, "GlobalServer"))
    sys.path.insert(0, os.path.join(REPO_ROOT, "ArtifactEvaluation", "ModelPlacement"))
    import VNode
    from global_server import GlobalServer
    from benchmark_utils import run_trace_benchmark
    from evaluation_utils import load_azure_trace
    from save_results import save_benchmark_results

    # Keep this run's logs inside the run directory:
    #   remote TensorStore / API server output → logs/<system>/remote/
    #   GlobalServer and Cluster logs          → logs/<system>/{GlobalServer,Cluster}.log
    VNode.LOG_BASE_DIR = log_dir
    file_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    for logger_name, filename in (("global_server", "GlobalServer.log"), ("VNode", "Cluster.log")):
        handler = logging.FileHandler(os.path.join(log_dir, filename))
        handler.setFormatter(file_formatter)
        logging.getLogger(logger_name).addHandler(handler)

    model_name = data["model"]
    pipelines = data["pipelines"]
    workload = cfg["workload"]
    dataset_path = os.path.join(REPO_ROOT, workload["dataset"])
    # The dataset file is the evaluation workload as is; warmup serves only its first seconds.
    trace_start = None
    trace_end = cfg["warmup"]["end_time_s"] if warmup else None
    node_ips = all_node_ips(cfg)

    # ─── Validate layer counts ───────────────────────────────────────
    expected_total_layers = sum(int(s[STAGE_LAYER_COUNT_IDX]) for s in pipelines[0]["stages"])
    for i, p in enumerate(pipelines):
        total_layers = sum(int(s[STAGE_LAYER_COUNT_IDX]) for s in p["stages"])
        assert total_layers == expected_total_layers, (
            f"P{i + 1} total layers ({total_layers}) != P1 total layers ({expected_total_layers})"
        )
    logger.info(f"Layer check passed: all pipelines have {expected_total_layers} layers")

    expected_requests = len(load_azure_trace(
        csv_path=dataset_path, start_time=trace_start, end_time=trace_end, logger=logger
    ))

    logger.info("Stopping leftover serving processes on the cluster nodes...")
    kill_remote_processes(node_ips, logger)

    global_server = GlobalServer()
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(pipelines))

    def abort(message):
        """Pipeline start-up failed: show why, clean up, and exit without waiting on stuck threads."""
        logger.error(message)
        print("\nRemote process logs:")
        tail_remote_logs(remote_log_dir)
        print(f"\nFull logs: {log_dir}")
        kill_remote_processes(node_ips, logger)
        logging.shutdown()
        sys.stdout.flush()
        os._exit(1)

    # ─── Launch pipelines ────────────────────────────────────────────
    pipeline_futures = {}
    for i, (p, mapping) in enumerate(zip(pipelines, mappings)):
        config = {
            "model_name": model_name,
            "total_num_layers": sum(int(s[STAGE_LAYER_COUNT_IDX]) for s in p["stages"]),
            "gpu_memory_utilization": p.get("gpu_memory_utilization", 0.85),
            "pp_layer_partition": p["pp_layer_partition"],
            "parallel_strategy": p["parallel_strategy"],
            "max_model_len": 8192,
            "max_num_batched_tokens": 8192,
            "max_num_seqs": 512,
            "model_source": "s3",
            "s3_path": f"s3://{cfg['s3_bucket']}/{model_name}",
            "num_gpu_blocks": p["num_blocks"],
            "max_batch_size": int(p["max_batch_size"]),
        }
        future = loop.run_in_executor(
            executor, global_server.create_pipeline,
            mapping, config, p["predicted_throughput_rps"], i + 1
        )
        pipeline_futures[future] = i + 1

    server_task = asyncio.create_task(global_server.run_global_server())

    try:
        # ─── Wait until every pipeline is ready ──────────────────────
        logger.info(f"Launching {len(pipelines)} pipeline(s): joining Ray, loading weights from S3, "
                    "starting inference engines...")
        start = time.time()
        pending = set(pipeline_futures)
        while pending:
            done, pending = await asyncio.wait(pending, timeout=HEARTBEAT_SECONDS)
            for future in done:
                if future.exception() is not None:
                    abort(f"Pipeline {pipeline_futures[future]} failed to start: {future.exception()!r}")
                logger.info(f"Pipeline {pipeline_futures[future]} is ready ({time.time() - start:.0f}s)")
            if pending:
                logger.info(f"  waiting for pipeline(s) {sorted(pipeline_futures[f] for f in pending)} "
                            f"[{time.time() - start:.0f}s] {latest_remote_log_line(remote_log_dir)}")
        logger.info(f"All pipelines are ready ({time.time() - start:.0f}s)")

        # ─── Benchmark ───────────────────────────────────────────────
        logger.info(f"Submitting {expected_requests} requests at once ({kind}, time_scale=0.0)...")
        metrics = await run_trace_benchmark(
            global_server=global_server,
            dataset_path=dataset_path,
            trace_output_prefix=f"{kind}_{system}",
            trace_base_dir=run_dir,
            num_requests=None,
            time_scale=0.0,
            model_name=model_name,
            percentiles=PERCENTILES,
            disable_tqdm=False,
            run_initial_test=False,
            start_time=trace_start,
            end_time=trace_end,
            logger=logger,
        )

        complete = metrics.completed == expected_requests
        save_benchmark_results(metrics, measured_path(run_dir, system, kind), extra={
            "system": SYSTEMS[system],
            "benchmark_type": kind,
            "num_pipelines": len(pipelines),
            "percentiles": PERCENTILES,
            "expected_requests": expected_requests,
            "complete": complete,
            "pipelines": [
                {"label": p["label"], "nodes": [ip for ip, _ in mapping],
                 "parallel_strategy": p["parallel_strategy"], "pp_layer_partition": p["pp_layer_partition"],
                 "max_batch_size": int(p["max_batch_size"])}
                for p, mapping in zip(pipelines, mappings)
            ],
        })

        banner(f"{SYSTEMS[system]} — " + ("warmup finished (not used for the evaluation)" if warmup
                                          else "offline throughput result"))
        print(f"  Completed requests      : {metrics.completed} / {expected_requests}")
        print(f"  Benchmark duration (s)  : {metrics.benchmark_duration:.1f}")
        print(f"  Request throughput      : {metrics.request_throughput:.3f} req/s")
        print(f"  Output token throughput : {metrics.output_throughput:.1f} tok/s")
        if not complete:
            logger.error("Not all requests completed" + ("." if warmup else
                                                         "; this run is NOT valid for the evaluation."))
        return 0 if complete else 1

    finally:
        logger.info("Cleaning up...")
        server_task.cancel()
        await asyncio.gather(server_task, return_exceptions=True)
        logger.info("Stopping pipelines...")
        try:
            global_server.cluster.stop_all_pipelines()
            logger.info("All pipelines stopped")
        except Exception as e:
            logger.error(f"Error stopping pipelines: {e}")
        kill_remote_processes(node_ips, logger)
        executor.shutdown(wait=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--system", choices=list(SYSTEMS), required=True)
    parser.add_argument("--run-dir", default=None, help="Run directory (default: results/latest)")
    parser.add_argument("--warmup", action="store_true",
                        help="Launch the pipelines and serve only the short warmup trace window")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the pipelines and node mapping, then exit without launching anything")
    args = parser.parse_args()

    run_dir = resolve_run_dir(args.run_dir)
    cfg = load_config()
    data = load_placement(run_dir, args.system)

    problems = validate_nodes(cfg)
    if problems and not args.dry_run:
        sys.exit("config.json: " + "; ".join(problems))
    mappings = assign_nodes(data["pipelines"], cfg)

    banner(("Warmup" if args.warmup else "Offline benchmark") + f" — {SYSTEMS[args.system]}")
    print_plan(args.system, data, mappings)
    if args.dry_run:
        return
    require_ssh(all_node_ips(cfg))   # fail early and clearly if a node is down

    try:
        exit_code = asyncio.run(run(args.system, run_dir, cfg, data, mappings, warmup=args.warmup))
    except KeyboardInterrupt:
        # Pipeline launch threads may still be blocked waiting on remote services;
        # exit without joining them.
        print("\nInterrupted by user.")
        exit_code = 130
    logging.shutdown()
    sys.stdout.flush()
    os._exit(exit_code)


if __name__ == "__main__":
    main()
