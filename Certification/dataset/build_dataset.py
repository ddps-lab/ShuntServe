"""
Build the evaluation datasets from the original Azure LLM Inference Conversation Trace (2023).

  1. Drop requests whose input exceeds 2048 tokens (ContextTokens > 2048).
  2. Keep the requests that arrive within the first N seconds of the trace,
     for each window in WINDOWS_SECONDS (3 minutes and 5 minutes).

One file per window is written next to this script. Rows are copied unchanged
(TIMESTAMP, ContextTokens, GeneratedTokens), in their original order.

    python3 build_dataset.py
"""
import csv
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "..", "..", "ArtifactEvaluation", "Datasets", "AzureLLMInferenceTrace_conv.csv")

MAX_INPUT_TOKENS = 2048
WINDOWS_SECONDS = [180, 300]


def output_path(window_seconds):
    return os.path.join(HERE, f"azure_conv_2023_input{MAX_INPUT_TOKENS}_first{window_seconds}s.csv")


def parse_timestamp(text):
    # The trace has 7 fractional digits; Python parses at most 6.
    head, _, fraction = text.partition(".")
    return datetime.fromisoformat(f"{head}.{fraction[:6]}" if fraction else head)


def main():
    with open(SOURCE) as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)

    kept = [r for r in rows if int(r["ContextTokens"]) <= MAX_INPUT_TOKENS]
    start = parse_timestamp(kept[0]["TIMESTAMP"])
    print(f"Source                     : {os.path.basename(SOURCE)} ({len(rows)} requests)")
    print(f"After input <= {MAX_INPUT_TOKENS} tokens : {len(kept)} requests")

    for window_seconds in WINDOWS_SECONDS:
        window = [r for r in kept
                  if (parse_timestamp(r["TIMESTAMP"]) - start).total_seconds() <= window_seconds]
        with open(output_path(window_seconds), "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(window)

        inputs = [int(r["ContextTokens"]) for r in window]
        outputs = [int(r["GeneratedTokens"]) for r in window]
        print(f"\nFirst {window_seconds} seconds → {os.path.basename(output_path(window_seconds))}")
        print(f"  Requests                 : {len(window)}")
        print(f"  Input tokens  (mean/max) : {sum(inputs) / len(inputs):.1f} / {max(inputs)}")
        print(f"  Output tokens (mean/max) : {sum(outputs) / len(outputs):.1f} / {max(outputs)}")
        print(f"  Total tokens  (in/out)   : {sum(inputs)} / {sum(outputs)}")


if __name__ == "__main__":
    main()
