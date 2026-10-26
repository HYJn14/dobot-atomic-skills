"""Local contract checks for the 3.2 -> 3.3 unified upload fixtures."""

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "docs" / "execution-api" / "unified-upload" / "fixtures"
SCRIPT = ROOT / "scripts" / "upload_robot_execution.py"


def read(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8-sig"))


@pytest.mark.parametrize("name,status,event_count,log_count", [
    ("robot_execution_succeeded.json", "succeeded", 47, 40),
    ("robot_execution_failed.json", "failed", 32, 28),
])
def test_fixture_matches_unified_upload_contract(name, status, event_count, log_count):
    payload = read(name)
    assert payload["schema_version"] == "dobot.execution.v1"
    assert payload["execution"]["status"] == status
    assert len(payload["events"]) == event_count
    assert len(payload["atomic_logs"]) == log_count
    event_ids = {event["event_id"] for event in payload["events"]}
    assert payload["execution"]["terminal_event_id"] in event_ids


def test_uploader_validates_fixtures_without_network():
    import importlib.util

    spec = importlib.util.spec_from_file_location("upload_robot_execution", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    for name in ("robot_execution_succeeded.json", "robot_execution_failed.json"):
        errors = module.validate_payload(read(name))
        assert errors == []


def test_uploader_rejects_invalid_execution_status():
    import importlib.util

    spec = importlib.util.spec_from_file_location("upload_robot_execution", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    payload = read("robot_execution_succeeded.json")
    payload["execution"]["status"] = "not-a-status"
    assert "/execution/status" in " ".join(module.validate_payload(payload))


def test_receipt_validation_requires_matching_identity_and_hash():
    import hashlib
    import importlib.util

    spec = importlib.util.spec_from_file_location("upload_robot_execution", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    payload = read("robot_execution_succeeded.json")
    body = module.request_bytes(payload)
    receipt = {
        "receipt_id": "receipt-1",
        "message_id": payload["message_id"],
        "execution_id": payload["identity"]["execution_id"],
        "stored": True,
        "duplicate": False,
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "warnings": [],
    }
    assert module.validate_receipt(receipt, payload, body) == []
    receipt["message_id"] = "wrong"
    assert module.validate_receipt(receipt, payload, body)


def test_receipt_validation_rejects_integer_boolean_and_empty_receipt_id():
    import hashlib
    import importlib.util

    spec = importlib.util.spec_from_file_location("upload_robot_execution", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    payload = read("robot_execution_succeeded.json")
    body = module.request_bytes(payload)
    receipt = {
        "receipt_id": "",
        "message_id": payload["message_id"],
        "execution_id": payload["identity"]["execution_id"],
        "stored": True,
        "duplicate": 1,
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "warnings": [],
    }
    errors = module.validate_receipt(receipt, payload, body)
    assert "/receipt_id" in " ".join(errors)
    assert "/duplicate" in " ".join(errors)


def test_validator_rejects_invalid_task_outcome():
    import importlib.util

    spec = importlib.util.spec_from_file_location("upload_robot_execution", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    payload = read("robot_execution_succeeded.json")
    payload["result"]["task_outcome"] = "bogus"
    assert "/result/task_outcome" in " ".join(module.validate_payload(payload))
