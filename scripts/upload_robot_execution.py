#!/usr/bin/env python3
"""Validate and upload a simulated robot execution handoff.

The script uses only the Python standard library so it can run from the
dobot skill checkout without adding a dependency. It never talks to the
robot; it only uploads an already-created execution record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "docs" / "execution-api" / "unified-upload" / "fixtures"
DEFAULT_BASE_URL = os.environ.get(
    "ROBOT_EXECUTION_BASE_URL", "http://100.79.134.36:8000"
).rstrip("/")
ENDPOINT = "/api/v1/robot-executions"
EXECUTION_STATUSES = {"succeeded", "failed", "cancelled", "interrupted", "unknown"}
TASK_OUTCOMES = {"met", "not_met", "unknown"}
AVAILABILITY_STATUSES = {
    "available", "partial", "not_collected", "not_applicable", "unknown"
}
AVAILABILITY_KEYS = {
    "task_targets", "recipe", "device_configuration", "plan", "execution_events",
    "atomic_logs", "command_feedback", "task_effect", "media", "collection_quality",
}

REQUIRED_TOP_LEVEL = {
    "schema_version",
    "message_id",
    "producer",
    "created_at",
    "identity",
    "execution",
    "devices",
    "task",
    "plan",
    "events",
    "atomic_logs",
    "result",
    "data_availability",
    "source_documents",
    "attachments",
    "extensions",
}


def fixture_path(name: str) -> Path:
    choices = {
        "success": FIXTURE_DIR / "robot_execution_succeeded.json",
        "failure": FIXTURE_DIR / "robot_execution_failed.json",
    }
    return choices[name]


def load_payload(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    return payload


def validate_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_TOP_LEVEL - payload.keys())
    errors.extend(f"missing top-level field: /{field}" for field in missing)
    if payload.get("schema_version") != "dobot.execution.v1":
        errors.append("/schema_version must be 'dobot.execution.v1'")
    execution = payload.get("execution")
    if not isinstance(execution, dict):
        errors.append("/execution must be an object")
    elif execution.get("status") not in EXECUTION_STATUSES:
        errors.append(
            "/execution/status must be one of succeeded/failed/cancelled/interrupted/unknown"
        )
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        errors.append("/events must be a non-empty array")
    elif any(
        not isinstance(event, dict)
        or not isinstance(event.get("event_id"), str)
        or not event["event_id"]
        for event in events
    ):
        errors.append("/events entries must be objects with non-empty string event_id")
    elif len({event["event_id"] for event in events}) != len(events):
        errors.append("/events/event_id values must be unique")
    if not isinstance(payload.get("atomic_logs"), list):
        errors.append("/atomic_logs must be an array")
    terminal_id = execution.get("terminal_event_id") if isinstance(execution, dict) else None
    event_ids = {event.get("event_id") for event in payload.get("events", []) if isinstance(event, dict)}
    if terminal_id and terminal_id not in event_ids:
        errors.append("/execution/terminal_event_id does not reference /events")
    result = payload.get("result")
    if not isinstance(result, dict):
        errors.append("/result must be an object")
    else:
        if result.get("task_outcome") not in TASK_OUTCOMES:
            errors.append("/result/task_outcome must be met/not_met/unknown")
        if not isinstance(result.get("physical_success_verified"), (bool, type(None))):
            errors.append("/result/physical_success_verified must be boolean or null")
        if not isinstance(result.get("measurements"), list):
            errors.append("/result/measurements must be an array")
        if not isinstance(result.get("judgments"), list):
            errors.append("/result/judgments must be an array")
        else:
            for index, judgment in enumerate(result["judgments"]):
                if not isinstance(judgment, dict):
                    errors.append(f"/result/judgments/{index} must be an object")
                elif judgment.get("status") not in TASK_OUTCOMES:
                    errors.append(f"/result/judgments/{index}/status is invalid")
    availability = payload.get("data_availability")
    if not isinstance(availability, dict):
        errors.append("/data_availability must be an object")
    else:
        for key in sorted(AVAILABILITY_KEYS - availability.keys()):
            errors.append(f"missing /data_availability/{key}")
        for key in AVAILABILITY_KEYS & availability.keys():
            value = availability[key]
            if not isinstance(value, dict):
                errors.append(f"/data_availability/{key} must be an object")
            elif value.get("status") not in AVAILABILITY_STATUSES:
                errors.append(f"/data_availability/{key}/status is invalid")
    if payload.get("attachments") != []:
        errors.append("/attachments must be an empty array for v1")
    return errors


def request_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def validate_receipt(response: Any, payload: dict[str, Any], body: bytes) -> list[str]:
    if not isinstance(response, dict):
        return ["response must be a JSON object"]
    required = {
        "receipt_id",
        "message_id",
        "execution_id",
        "stored",
        "duplicate",
        "content_sha256",
        "warnings",
    }
    errors = [f"response missing field: /{field}" for field in sorted(required - response.keys())]
    if not isinstance(response.get("receipt_id"), str) or not response["receipt_id"]:
        errors.append("response /receipt_id must be a non-empty string")
    if response.get("message_id") != payload.get("message_id"):
        errors.append("response /message_id does not match request")
    if response.get("execution_id") != payload.get("identity", {}).get("execution_id"):
        errors.append("response /execution_id does not match request")
    if response.get("stored") is not True:
        errors.append("response /stored must be true")
    if not isinstance(response.get("duplicate"), bool):
        errors.append("response /duplicate must be boolean")
    expected_hash = hashlib.sha256(body).hexdigest()
    if response.get("content_sha256") != expected_hash:
        errors.append("response /content_sha256 does not match request bytes")
    if not isinstance(response.get("warnings"), list):
        errors.append("response /warnings must be an array")
    return errors


class UploadError(RuntimeError):
    """A transport failure that did not produce an HTTP response."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward an Authorization header to a redirect target."""

    def redirect_request(self, request, fp, code, msg, headers, new_url):
        return None


def upload(
    payload: dict[str, Any],
    base_url: str,
    timeout: float,
    retries: int,
) -> tuple[int, Any, list[str]]:
    body = request_bytes(payload)
    if os.environ.get("ROBOT_EXECUTION_BEARER_TOKEN") and not base_url.lower().startswith("https://"):
        raise UploadError("refusing to send ROBOT_EXECUTION_BEARER_TOKEN over non-HTTPS")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{ENDPOINT}",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    token = os.environ.get("ROBOT_EXECUTION_BEARER_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(NoRedirectHandler())
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read()
                decoded = decode_response(raw)
                return response.status, decoded, validate_receipt(decoded, payload, body)
        except urllib.error.HTTPError as error:
            decoded = decode_response(error.read())
            if error.code < 500 or attempt >= retries:
                return error.code, decoded, []
            last_error = error
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            last_error = error
            if attempt >= retries:
                break
        time.sleep(0.5 * (2**attempt))
    raise UploadError(f"upload failed after {retries + 1} attempts: {last_error}")


def decode_response(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", choices=("success", "failure"), default="success")
    parser.add_argument("--file", type=Path, help="upload a custom JSON file")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, choices=range(3), default=2)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="validate and print request metadata")
    args = parser.parse_args()

    if args.timeout <= 0 or args.retries < 0:
        print("--timeout must be > 0 and --retries must be >= 0", file=sys.stderr)
        return 2
    path = args.file or fixture_path(args.fixture)
    try:
        payload = load_payload(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"cannot load payload: {error}", file=sys.stderr)
        return 2
    errors = validate_payload(payload)
    if errors:
        for error in errors:
            print(f"INVALID: {error}", file=sys.stderr)
        return 2

    execution = payload["execution"]
    print(f"fixture={path}")
    print(f"status={execution['status']} events={len(payload['events'])} atomic_logs={len(payload['atomic_logs'])}")
    print(f"target={args.base_url.rstrip('/')}{ENDPOINT}")
    if args.validate_only or args.dry_run:
        print("validation=ok")
        return 0

    try:
        status, response, receipt_errors = upload(
            payload, args.base_url, args.timeout, args.retries
        )
    except UploadError as error:
        print(f"upload error: {error}", file=sys.stderr)
        return 1
    print(f"http_status={status}")
    print(json.dumps(response, ensure_ascii=False, indent=2))
    if 200 <= status < 300 and receipt_errors:
        for error in receipt_errors:
            print(f"INVALID_RECEIPT: {error}", file=sys.stderr)
        return 1
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
