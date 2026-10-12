"""Mock-only partner client: catalog -> package -> dispatch -> status/events."""

import argparse
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

from .upstream import Upstream


def demo_package(revision, run_id, hold=1):
    def action(node, skill, arm, obj, location):
        return {"node_id": node, "node_type": "action", "status": "pending", "timeout_s": 180,
                "skill_invocation": {"skill_id": "embodied." + skill, "skill_version": "1.0.0",
                    "device_id": "dobot-nova-" + arm, "idempotency_key": run_id + ":" + node,
                    "parameters": {"object_id": obj, "source_ref" if skill == "pick" else "destination_ref": location}},
                "on_fail": {"strategy": "decision_agent", "max_retries": 0,
                            "allowed_actions": ["safe_stop", "human_confirmation"]}}
    nodes = [action("pick-A", "pick", "left", "A", "object:A"),
             action("pick-B", "pick", "right", "B", "object:B"),
             {"node_id": "hold-A", "node_type": "wait", "status": "pending", "duration_s": hold,
              "timeout_s": 60, "on_fail": {"strategy": "safe_stop", "max_retries": 0}},
             action("place-A", "place", "left", "A", "target:target_A"),
             action("place-B", "place", "right", "B", "target:target_B"),
             action("repick-B", "pick", "right", "B", "target:target_B"),
             action("return-B", "place", "right", "B", "object:B")]
    edges = [("pick-A", "hold-A"), ("hold-A", "place-A"), ("pick-B", "place-B"),
             ("place-A", "repick-B"), ("place-B", "repick-B"), ("repick-B", "return-B")]
    return {"schema_version": "2.1", "graph_version": "0.1", "package_id": "runtime-" + run_id,
            "run_id": run_id, "experiment_id": "experiment-" + run_id, "recipe_id": "dobot-hold-return",
            "version": "1.0", "catalog_revision": revision, "entry_node_ids": ["pick-A", "pick-B"],
            "nodes": nodes, "edges": [{"source": a, "target": b, "edge_type": "success"} for a, b in edges]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Exercise the partner protocol against a MOCK gateway only")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--two-phase", action="store_true")
    parser.add_argument("--package", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--write-template", type=Path, help="Write an offline example with placeholder catalog revision")
    args = parser.parse_args(argv)
    if args.write_template:
        args.write_template.parent.mkdir(parents=True, exist_ok=True)
        args.write_template.write_text(json.dumps(demo_package("CATALOG_REVISION_FROM_GATEWAY", "example-001"), indent=2), encoding="utf-8")
        return 0
    token = os.environ.get("DOBOT_GATEWAY_TOKEN")
    transport = Upstream(args.url, token)

    def request(path, body=None, key=None):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if key:
            headers["Idempotency-Key"] = key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, allow_nan=False).encode("utf-8")
        try:
            with transport.opener.open(Request(transport.base_url + path, data=data, headers=headers), timeout=5) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError("Gateway response exceeds size limit")
                return json.loads(raw)
        except HTTPError as error:
            with error:
                raise RuntimeError("HTTP %s: %s" % (error.code, error.read(4096).decode("utf-8"))) from error

    catalog = request("/v1/catalog")
    if catalog["execution_profile"]["mode"] != "mock":
        parser.error("This demo client only dispatches to mock mode")
    run_id = "demo-" + uuid4().hex
    graph = json.loads(args.package.read_text(encoding="utf-8")) if args.package else demo_package(catalog["catalog_revision"], run_id)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"graph": graph}, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.two_phase:
        validation = request("/device-gateway/v1/execution-plans/validate", {"package": graph}, "preflight-" + run_id)
        result = request("/device-gateway/v1/executions", {"package": graph,
                         "validation_token": validation["validation_token"]}, "dispatch-" + run_id)
    else:
        result = request("/v1/execute-graph", {"graph": graph}, "dispatch-" + run_id)
    execution_id = result["execution_id"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = request("/v1/executions/" + execution_id)
        if status["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            events = request("/v1/executions/" + execution_id + "/events")
            print(json.dumps({"execution": status, "event_count": len(events["events"])}, ensure_ascii=False, indent=2))
            return 0 if status["status"] == "succeeded" else 1
        time.sleep(0.1)
    raise TimeoutError("Execution still running; query its ID before any retry: " + execution_id)


if __name__ == "__main__":
    raise SystemExit(main())
