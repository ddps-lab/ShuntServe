"""
Write the worker private IPs from `terraform output` into ../config.json ("nodes").

Run after `terraform apply`:
    python3 fill_config.py
"""
import json
import os
import subprocess
import sys

IAC_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(IAC_DIR, "..", "config.json")


def main():
    result = subprocess.run(["terraform", "output", "-json", "nodes"],
                            cwd=IAC_DIR, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"`terraform output` failed (run `terraform apply` first):\n{result.stderr.strip()}")
    nodes = json.loads(result.stdout)

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    expected = {itype: len(ips) for itype, ips in config["nodes"].items()}
    actual = {itype: len(ips) for itype, ips in nodes.items()}
    if expected != actual:
        sys.exit(f"Cluster shape mismatch.\n  config.json : {expected}\n  terraform   : {actual}\n"
                 "Make instance_type_count and config.json agree, then re-run.")

    # Keep config.json's instance-type order
    config["nodes"] = {itype: nodes[itype] for itype in config["nodes"]}
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    print(f"Updated {os.path.normpath(CONFIG_PATH)}:")
    for itype, ips in config["nodes"].items():
        print(f"  {itype:<12} {', '.join(ips)}")


if __name__ == "__main__":
    main()
