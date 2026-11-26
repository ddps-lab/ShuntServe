"""
Step 4 — Compare the two serving configurations and write the evaluation report.

Reads <run-dir>/placement/ and <run-dir>/measured/, computes the throughput
improvement of the heterogeneous configuration (ShuntServe) over the baseline
(vLLM, homogeneous pipelines) on the same cluster, and writes
<run-dir>/report.md and <run-dir>/report.json.

With --print-only, nothing is written: the summary is printed from an existing run
directory (used by show_result.sh).

The summary shows the throughput ratio and improvement only; whether the target
is met is recorded in report.json ("verdict") but not printed.

Exit code: 0 = valid result, 2 = results missing or invalid (not all requests completed).
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (REPO_ROOT, SYSTEMS, banner, cluster_description, load_run_config, measured_path,
                    placement_path, plain_instance_type, resolve_run_dir)

sys.path.insert(0, os.path.join(REPO_ROOT, "ModelPlacement"))
from hardware_specs import INSTANCE_SPEC  # noqa: E402

ROLE = {"vllm": "기준 구성 (동종 GPU 파이프라인)", "shuntserve": "이기종 GPU 구성 (ShuntServe)"}


def load_json(path):
    if not os.path.exists(path):
        print(f"Missing result file: {path}")
        sys.exit(2)
    with open(path) as f:
        return json.load(f)


def describe_stages(pipeline):
    """'g6.12xlarge×2 + g6e.xlarge×4': instance types in order of first appearance, with node counts."""
    counts = Counter(plain_instance_type(s[0]) for s in pipeline["stages"])
    order = list(dict.fromkeys(plain_instance_type(s[0]) for s in pipeline["stages"]))
    return " + ".join(f"{t}×{counts[t]}" for t in order)


def gpu_types(pipeline):
    return list(dict.fromkeys(INSTANCE_SPEC[plain_instance_type(s[0])]["gpu_type"] for s in pipeline["stages"]))


def write_report_files(run_dir, lines, summary):
    with open(os.path.join(run_dir, "report.md"), "w") as f:
        f.write("\n".join(lines))
    with open(os.path.join(run_dir, "report.json"), "w") as f:
        json.dump(summary, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", default=None, help="Run directory (default: results/latest)")
    parser.add_argument("--print-only", action="store_true",
                        help="Print the summary without writing report.md / report.json")
    args = parser.parse_args()

    run_dir = resolve_run_dir(args.run_dir)
    cfg = load_run_config(run_dir)
    target = cfg["target_improvement_pct"]

    placement = {s: load_json(placement_path(run_dir, s)) for s in SYSTEMS}
    measured = {s: load_json(measured_path(run_dir, s)) for s in SYSTEMS}

    base, hetero = measured["vllm"], measured["shuntserve"]
    ratio = hetero["request_throughput"] / base["request_throughput"]
    improvement = (ratio - 1) * 100

    invalid = [SYSTEMS[s] for s in SYSTEMS if not measured[s].get("complete", False)]
    same_workload = base["expected_requests"] == hetero["expected_requests"]
    if invalid or not same_workload:
        verdict = "INVALID"
    else:
        verdict = "PASS" if improvement >= target else "FAIL"

    cluster_gpu_types = sorted({INSTANCE_SPEC[t]["gpu_type"] for t in cfg["nodes"]})
    workload = cfg["workload"]

    # ─── report.md ───────────────────────────────────────────────────
    lines = [
        "# 이기종 GPU 클러스터 LLM 서빙 처리량 향상율 평가 결과",
        "",
        f"- 평가 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 실행 ID: `{os.path.basename(run_dir)}`",
        "",
        "## 평가 조건",
        "",
        "| 항목 | 내용 |",
        "|---|---|",
        f"| 모델 | {cfg['model_name']} |",
        f"| 클러스터 (두 구성 공통, 동일 예산) | {cluster_description(cfg)} |",
        f"| GPU 종류 | {', '.join(cluster_gpu_types)} ({len(cluster_gpu_types)}종) |",
        f"| 데이터셋 | Azure LLM Inference Conversation Trace 2023 (`{os.path.basename(workload['dataset'])}`) |",
        f"| 요청 | 입력 2048 토큰 초과 요청을 제외한 trace의 처음 {workload['window_s']}초 구간, "
        f"{base['expected_requests']}개를 한 번에 제출 (오프라인 처리량) |",
        "",
        "## 서빙 구성",
        "",
    ]
    for s in ("vllm", "shuntserve"):
        lines += [f"### {ROLE[s]}", "",
                  "| 파이프라인 | 인스턴스 | GPU | TP | 레이어 분할 | 최대 배치 |",
                  "|---|---|---|---|---|---|"]
        for p in placement[s]["pipelines"]:
            lines.append(f"| {p['label']} | {describe_stages(p)} | {' + '.join(gpu_types(p))} | "
                         f"{p['parallel_strategy']} | {p['pp_layer_partition']} | {int(p['max_batch_size'])} |")
        lines.append("")

    lines += [
        "## 측정 결과",
        "",
        "| 구성 | 완료 요청 | 소요 시간 (s) | 처리량 (req/s) | 출력 토큰 처리량 (tok/s) |",
        "|---|---|---|---|---|",
    ]
    for s in ("vllm", "shuntserve"):
        m = measured[s]
        lines.append(f"| {ROLE[s]} | {m['completed']} / {m['expected_requests']} | {m['benchmark_duration']:.1f} | "
                     f"**{m['request_throughput']:.3f}** | {m['output_throughput']:.1f} |")
    lines += [
        "",
        "## 결과",
        "",
        f"- 처리량 비율: {hetero['request_throughput']:.3f} / {base['request_throughput']:.3f} = **{ratio:.3f}배**",
        f"- 처리량 향상율: **{improvement:.1f}%**",
    ]
    if invalid:
        lines.append(f"- 무효 사유: {', '.join(invalid)} 실행에서 모든 요청이 완료되지 않음")
    if not same_workload:
        lines.append("- 무효 사유: 두 구성의 요청 수가 다름")
    lines.append("")

    if not args.print_only:
        write_report_files(run_dir, lines, {
            "verdict": verdict,
            "target_improvement_pct": target,
            "improvement_pct": improvement,
            "throughput_ratio": ratio,
            "baseline_throughput_rps": base["request_throughput"],
            "heterogeneous_throughput_rps": hetero["request_throughput"],
            "requests": base["expected_requests"],
            "cluster": {t: len(ips) for t, ips in cfg["nodes"].items()},
            "model": cfg["model_name"],
        })

    # ─── Console summary ─────────────────────────────────────────────
    banner("Evaluation result — throughput improvement with heterogeneous GPU pipelines")
    print(f"Cluster (same for both): {cluster_description(cfg)}")
    print(f"Requests: {base['expected_requests']} (Azure conversation trace, submitted at once)\n")
    print(f"  {'Configuration':<34}{'Pipelines':>10}{'Completed':>12}{'Duration(s)':>13}{'Throughput(req/s)':>20}")
    for s, name in (("vllm", "Baseline: vLLM (homogeneous)"), ("shuntserve", "ShuntServe (heterogeneous)")):
        m = measured[s]
        print(f"  {name:<34}{m['num_pipelines']:>10}{m['completed']:>12}{m['benchmark_duration']:>13.1f}"
              f"{m['request_throughput']:>20.3f}")
    print(f"\n  Throughput ratio       : {ratio:.3f}x")
    print(f"  Throughput improvement : {improvement:.1f}%")
    if verdict == "INVALID":
        reason = (f"not all requests completed ({', '.join(invalid)})" if invalid
                  else "the two runs used different numbers of requests")
        print(f"\n  WARNING: this result is not valid: {reason}")
    print(f"\nReport: {os.path.join(run_dir, 'report.md')}")

    sys.exit(2 if verdict == "INVALID" else 0)


if __name__ == "__main__":
    main()
