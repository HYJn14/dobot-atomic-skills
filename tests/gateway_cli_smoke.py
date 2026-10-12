"""Run both documented CLI protocols on ephemeral mock servers, then stop them."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    outcomes = {}
    for protocol in ("single", "two-phase"):
        with TemporaryDirectory() as temp:
            stdout = Path(temp) / "server.log"
            with stdout.open("w", encoding="utf-8") as log:
                process = subprocess.Popen([sys.executable, "-B", "-m", "task_skills.run_gateway", "--mode", "mock",
                                            "--port", "0", "--data-dir", str(Path(temp) / "data")],
                                           cwd=str(root), stdout=log, stderr=log,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                try:
                    deadline = time.monotonic() + 15
                    address = None
                    while time.monotonic() < deadline:
                        match = re.search(r"Dobot gateway: 127\.0\.0\.1:(\d+) mode=mock", stdout.read_text(encoding="utf-8"))
                        if match:
                            address = "http://127.0.0.1:" + match[1]
                            break
                        if process.poll() is not None:
                            raise RuntimeError(stdout.read_text(encoding="utf-8"))
                        time.sleep(0.05)
                    if address is None:
                        raise TimeoutError("Mock CLI server did not start")
                    command = [sys.executable, "-B", "-m", "task_skills.gateway.demo_client", "--url", address,
                               "--output", str(args.output / (protocol + "-request.json"))]
                    if protocol == "two-phase":
                        command.append("--two-phase")
                    result = subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=45)
                    if result.returncode:
                        raise RuntimeError(result.stderr + result.stdout)
                    summary = json.loads(result.stdout)
                    if (summary["execution"]["status"] != "succeeded" or summary["execution"]["mode"] != "mock"
                            or len(summary["execution"]["node_statuses"]) != 7 or summary["event_count"] != 16):
                        raise AssertionError(summary)
                    outcomes[protocol] = summary
                    (args.output / (protocol + "-result.json")).write_text(json.dumps(summary, indent=2), encoding="utf-8")
                finally:
                    # These are mock-only child processes, with no hardware imports.
                    process.terminate()
                    process.wait(timeout=15)
    print(json.dumps({key: {"status": value["execution"]["status"], "nodes": 7, "events": value["event_count"]}
                      for key, value in outcomes.items()}, indent=2))


if __name__ == "__main__":
    main()
