"""One-command P0 acceptance gate. Run with the project development dependencies."""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(args, cwd=ROOT, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="isolated API address for live case replay")
    parser.add_argument("--db", type=Path, help="isolated API SQLite file")
    parser.add_argument("--token", help="demo-user Bearer token")
    parser.add_argument("--restart-command", help="controlled restart of an isolated test deployment")
    parser.add_argument("--report", type=Path, help="live replay JSON report")
    args = parser.parse_args()
    if bool(args.base_url) != bool(args.db):
        parser.error("--base-url and --db must be provided together")
    commands = [
        [sys.executable, "evaluation/datasets/dian_logistics_v1/validate_dataset.py"],
        [sys.executable, "-m", "pytest", "-q", "tests/test_logistics_cases.py",
         "tests/test_logistics_api.py", "tests/test_logistics_eval_runner.py"],
    ]
    if args.base_url:
        command = [sys.executable, "-m", "evaluation.logistics_eval_runner", "--split", "dev",
                   "--base-url", args.base_url, "--db", str(args.db)]
        if args.token:
            command += ["--token", args.token]
        if args.restart_command:
            command += ["--restart-command", args.restart_command]
        if args.report:
            command += ["--output", str(args.report)]
        commands.append(command)
    failed = False
    for command in commands:
        failed |= run(command) != 0
    if not args.base_url:
        print("LIVE_REPLAY=NOT_RUN (provide --base-url and --db)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
