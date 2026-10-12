import copy
import json
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from task_skills.device_lease import DeviceLease
from task_skills.dobot_adapter import DobotHardwareAdapter
from task_skills.atomic_graph import AtomicNode
from task_skills.gateway.contracts import Catalog, ContractError
from task_skills.gateway.server import GatewayServer
from task_skills.gateway.service import Gateway
from task_skills.gateway.store import Store
from task_skills.gateway.upstream import DeliveryWorker, Upstream
from task_skills.gateway.operator import reset_scene
from task_skills.models import load_scene_config
from task_skills.run_graph import MockAdapter


SCENE = Path(__file__).resolve().parents[1] / "task_skills/scene_config.json"


def package(revision, suffix="1", hold=0):
    def action(node_id, skill, arm, obj, ref):
        return {"node_id": node_id, "node_type": "action", "status": "pending", "timeout_s": 30,
                "skill_invocation": {"skill_id": "embodied." + skill, "skill_version": "1.0.0",
                    "device_id": "dobot-nova-" + arm,
                    "parameters": {"object_id": obj, "source_ref" if skill == "pick" else "destination_ref": ref},
                    "idempotency_key": suffix + ":" + node_id},
                "on_fail": {"strategy": "decision_agent", "max_retries": 1, "allowed_actions": ["safe_stop", "human_confirmation"]}}
    nodes = [action("pick-A", "pick", "left", "A", "object:A"),
             action("pick-B", "pick", "right", "B", "object:B"),
             {"node_id": "hold-A", "node_type": "wait", "status": "pending", "duration_s": hold, "timeout_s": 5},
             action("place-A", "place", "left", "A", "target:target_A"),
             action("place-B", "place", "right", "B", "target:target_B"),
             action("repick-B", "pick", "right", "B", "target:target_B"),
             action("return-B", "place", "right", "B", "object:B")]
    edges = [("pick-A", "hold-A"), ("hold-A", "place-A"), ("pick-B", "place-B"),
             ("place-A", "repick-B"), ("place-B", "repick-B"), ("repick-B", "return-B")]
    return {"schema_version": "2.1", "graph_version": "0.1", "package_id": "runtime-" + suffix,
            "recipe_id": "pick-place", "version": "1.0", "experiment_id": "experiment-" + suffix,
            "catalog_revision": revision, "entry_node_ids": ["pick-A", "pick-B"], "nodes": nodes,
            "edges": [{"source": a, "target": b, "edge_type": "success"} for a, b in edges]}


class CompilerHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        data = json.dumps({"package": self.server.package}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.lock:
            self.server.received.append((self.path, body))
            status = 200
            if "/executions/" in self.path and self.server.reject_first_event:
                self.server.reject_first_event = False
                status = 404
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b'{"accepted": true}')


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.gateway = Gateway(load_scene_config(SCENE), Path(self.temp.name) / "data")
        self.server = GatewayServer(("127.0.0.1", 0), self.gateway, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.gateway.close()
        self.temp.cleanup()

    def request(self, path, body=None, key=None, authorized=True):
        headers = {"Authorization": "Bearer test-token"} if authorized else {}
        if key:
            headers["Idempotency-Key"] = key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        try:
            response = urlopen(Request(self.url + path, data=data, headers=headers), timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read())

    def submit(self, graph=None, key="dispatch-1"):
        return self.request("/v1/execute-graph", {"graph": graph or package(self.gateway.catalog.revision)}, key)

    def finish(self, execution_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.gateway.status(execution_id)
            if result["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return result
            time.sleep(0.01)
        self.fail("Execution did not terminate")

    def test_http_pipeline_bypasses_natural_language_and_preserves_join(self):
        with patch("task_skills.parser.parse_instruction", side_effect=AssertionError("NL parser must not run")):
            code, result = self.submit()
            self.assertEqual(code, 202)
            done = self.finish(result["execution_id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(set(done["node_statuses"].values()), {"succeeded"})
        events = self.gateway.store.events(result["execution_id"])
        completed = {e["node_id"]: e["sequence"] for e in events if e["event_type"] == "action_completed"}
        repick = next(e["sequence"] for e in events if e.get("node_id") == "repick-B" and e["event_type"] == "action_started")
        self.assertGreater(repick, max(completed["place-A"], completed["place-B"]))
        self.assertFalse(done["verification"]["physical_success_verified"])

    def test_duplicate_dispatch_returns_original_record_without_more_actions(self):
        adapters = []
        self.gateway.adapter_factory = lambda cancel: adapters.append(MockAdapter(cancel)) or adapters[-1]
        first = self.submit()[1]
        self.finish(first["execution_id"])
        second = self.submit()[1]
        self.assertEqual(first["execution_id"], second["execution_id"])
        self.assertEqual(len(adapters), 1)
        altered = package(self.gateway.catalog.revision)
        altered["nodes"][2]["duration_s"] = 0.1
        code, response = self.submit(altered)
        self.assertEqual((code, response["code"]), (409, "IDEMPOTENCY_CONFLICT"))

    def test_concurrent_duplicate_requests_start_one_worker(self):
        graph = package(self.gateway.catalog.revision, hold=0.1)
        adapters = []
        self.gateway.adapter_factory = lambda cancel: adapters.append(MockAdapter(cancel)) or adapters[-1]
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(lambda _: self.submit(graph), range(6)))
        self.assertEqual(len(responses), 6)
        self.assertTrue(all(code == 202 for code, _ in responses))
        results = [result for _, result in responses]
        self.assertEqual(len({r["execution_id"] for r in results}), 1)
        self.assertEqual(self.finish(results[0]["execution_id"])["status"], "succeeded")
        self.assertEqual(len(adapters), 1)

    def test_two_phase_reservation_is_package_bound_and_consumed_once(self):
        graph = package(self.gateway.catalog.revision)
        code, validation = self.request("/device-gateway/v1/execution-plans/validate", {"package": graph}, "pre-1")
        self.assertEqual(code, 200)
        code, blocked = self.submit()
        self.assertEqual((code, blocked["code"]), (409, "RESOURCE_CONFLICT"))
        changed = copy.deepcopy(graph)
        changed["nodes"][2]["duration_s"] = 0.1
        code, mismatch = self.request("/device-gateway/v1/executions", {
            "package": changed, "validation_token": validation["validation_token"]}, "dispatch-1")
        self.assertEqual(mismatch["code"], "VALIDATION_TOKEN_MISMATCH")
        body = {"package": graph, "validation_token": validation["validation_token"]}
        code, submitted = self.request("/device-gateway/v1/executions", body, "dispatch-1")
        self.assertEqual(code, 202)
        self.finish(submitted["execution_id"])
        self.assertEqual(self.request("/device-gateway/v1/executions", body, "dispatch-1")[1]["execution_id"], submitted["execution_id"])
        self.assertEqual(self.request("/device-gateway/v1/executions", body, "dispatch-2")[1]["code"], "VALIDATION_TOKEN_CONSUMED")

    def test_expired_reservation_refuses_formal_submission(self):
        self.gateway.reservation_seconds = 0.01
        graph = package(self.gateway.catalog.revision)
        validation = self.gateway.preflight({"package": graph}, "pre-1")
        time.sleep(0.03)
        with self.assertRaises(ContractError) as error:
            self.gateway.submit({"package": graph, "validation_token": validation["validation_token"]}, "d", True)
        self.assertEqual(error.exception.body["code"], "VALIDATION_TOKEN_EXPIRED")

    def test_cancel_wait_stops_downstream_and_returns_only_after_worker_finishes(self):
        graph = package(self.gateway.catalog.revision, hold=3)
        result = self.submit(graph)[1]
        execution_id = result["execution_id"]
        deadline = time.monotonic() + 2
        while self.gateway.status(execution_id)["node_statuses"]["hold-A"] != "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        code, response = self.request("/v1/executions/" + execution_id + "/cancel", {})
        self.assertEqual(code, 200)
        done = self.finish(execution_id)
        self.assertEqual(done["status"], "cancelled")
        self.assertEqual(done["node_statuses"]["place-A"], "blocked")
        self.assertIsNone(self.gateway.active)

    def test_failure_stops_other_branches_and_retains_actual_failed_node(self):
        class Failing(MockAdapter):
            def execute_atomic(self, node, scene):
                if node.arm == "left" and node.operation == "close_gripper":
                    raise RuntimeError("Injected controller failure")
                return super().execute_atomic(node, scene)
        self.gateway.adapter_factory = lambda cancel: Failing(cancel, 0.01)
        result = self.submit()[1]
        done = self.finish(result["execution_id"])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["node_statuses"]["pick-A"], "failed")
        self.assertEqual(done["node_statuses"]["repick-B"], "blocked")

    def test_skill_timeout_is_total_across_internal_actions(self):
        self.gateway.adapter_factory = lambda cancel: MockAdapter(cancel, 0.02)
        graph = package(self.gateway.catalog.revision)
        graph["nodes"][0]["timeout_s"] = 0.025
        result = self.submit(graph)[1]
        done = self.finish(result["execution_id"])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "MOTION_TIMEOUT")

    def test_unsupported_semantics_are_rejected_before_any_adapter(self):
        self.gateway.adapter_factory = lambda _: self.fail("Adapter constructed for invalid graph")
        mutations = [
            ("STALE_CATALOG_REVISION", lambda g: g.update(catalog_revision="old")),
            ("UNSUPPORTED_NODE_TYPE", lambda g: g["nodes"][0].update(node_type="observe")),
            ("UNSUPPORTED_EDGE_TYPE", lambda g: g["edges"][0].update(edge_type="failure")),
            ("UNSUPPORTED_SKILL", lambda g: g["nodes"][0]["skill_invocation"].update(skill_id="embodied.load")),
            ("UNSUPPORTED_CONDITION", lambda g: g["nodes"][0].update(preconditions=["object_detected"])),
            ("UNSUPPORTED_CONDITION", lambda g: g["nodes"][0].update(completion_condition="physical_grasp_verified")),
            ("UNSUPPORTED_RESUME", lambda g: g["nodes"][0].update(status="succeeded")),
            ("INVALID_GRAPH", lambda g: g["edges"].append({"source": "return-B", "target": "pick-B", "edge_type": "success"})),
            ("INVALID_GRAPH", lambda g: g["nodes"].append(copy.deepcopy(g["nodes"][0]))),
            ("DEVICE_INCOMPATIBLE", lambda g: g["nodes"][0]["skill_invocation"]["parameters"].update(object_id="B")),
        ]
        for expected, change in mutations:
            with self.subTest(expected=expected):
                graph = package(self.gateway.catalog.revision)
                change(graph)
                code, response = self.submit(graph)
                self.assertGreaterEqual(code, 400)
                self.assertEqual(response["code"], expected)

    def test_node_array_order_does_not_change_dependencies(self):
        graph = package(self.gateway.catalog.revision)
        graph["nodes"].reverse()
        result = self.submit(graph)[1]
        self.assertEqual(self.finish(result["execution_id"])["status"], "succeeded")

    def test_same_arm_unordered_actions_and_invalid_object_lifecycle_rejected(self):
        graph = package(self.gateway.catalog.revision)
        graph["edges"] = [e for e in graph["edges"] if e["target"] != "place-A"]
        graph["entry_node_ids"].append("place-A")
        self.assertEqual(self.submit(graph)[1]["code"], "RESOURCE_CONFLICT")
        graph = package(self.gateway.catalog.revision)
        graph["nodes"][0]["skill_invocation"].update(skill_id="embodied.place")
        graph["nodes"][0]["skill_invocation"]["parameters"] = {"object_id": "A", "destination_ref": "target:target_A"}
        self.assertEqual(self.submit(graph)[1]["code"], "INVALID_OBJECT_STATE")

    def test_new_dispatch_key_cannot_replay_previously_used_node_keys(self):
        first = self.submit()[1]
        self.finish(first["execution_id"])
        graph = package(self.gateway.catalog.revision)
        graph["nodes"][0]["skill_invocation"]["parameters"]["source_ref"] = "target:target_A"
        self.assertEqual(self.submit(graph, key="different")[1]["code"], "IDEMPOTENCY_CONFLICT")

    def test_successful_object_state_is_preserved_for_the_next_graph(self):
        first = self.submit()[1]
        self.finish(first["execution_id"])
        graph = package(self.gateway.catalog.revision, "next")
        self.assertEqual(self.submit(graph, "next")[1]["code"], "INVALID_OBJECT_STATE")
        graph["nodes"][0]["skill_invocation"]["parameters"]["source_ref"] = "target:target_A"
        result = self.submit(graph, "next")[1]
        self.assertEqual(self.finish(result["execution_id"])["status"], "succeeded")

    def test_large_integer_parameter_returns_validation_error(self):
        graph = package(self.gateway.catalog.revision)
        graph["nodes"][0]["timeout_s"] = 10 ** 400
        code, response = self.submit(graph)
        self.assertEqual((code, response["code"]), (422, "DEVICE_PARAMETER_OUT_OF_RANGE"))
        result = self.submit()[1]
        code, response = self.request("/v1/executions/" + result["execution_id"] + "/events?after=" + str(10 ** 100))
        self.assertEqual((code, response["code"]), (400, "INVALID_REQUEST"))

    def test_unconfirmed_stop_quarantines_and_blocks_new_work(self):
        class Unconfirmed(MockAdapter):
            def execute_atomic(self, node, scene):
                raise RuntimeError("Injected motion fault")
            def stop_and_verify(self):
                return {"confirmed": False}
        self.gateway.adapter_factory = lambda cancel: Unconfirmed(cancel)
        result = self.submit()[1]
        done = self.finish(result["execution_id"])
        self.assertEqual((done["status"], done["error"]["code"]), ("interrupted", "STOP_UNCONFIRMED"))
        self.assertEqual(self.submit(package(self.gateway.catalog.revision, "2"), "2")[1]["code"], "STOP_UNCONFIRMED")

    def test_mock_independent_arm_skills_execute_concurrently(self):
        barrier = threading.Barrier(2)
        class Parallel(MockAdapter):
            def execute_atomic(self, node, scene):
                if node.node_id in {"pick-A:0", "pick-B:0"}:
                    barrier.wait(timeout=2)
                return super().execute_atomic(node, scene)
        self.gateway.adapter_factory = lambda cancel: Parallel(cancel)
        result = self.submit()[1]
        self.assertEqual(self.finish(result["execution_id"])["status"], "succeeded")

    def test_authentication_request_shape_and_resume_rejected(self):
        self.assertEqual(self.request("/v1/catalog", authorized=False)[0], 401)
        graph = package(self.gateway.catalog.revision)
        self.assertEqual(self.request("/v1/execute-graph", {"graph": graph})[0], 422)
        self.assertEqual(self.request("/v1/execute-graph", {"graph": graph, "resume_from_node_id": "pick-A"}, "x")[1]["code"], "UNSUPPORTED_RESUME")
        self.assertEqual(self.request("/v1/execute-graph", {"graph": graph, "mode": "live"}, "x")[1]["code"], "INVALID_REQUEST")

    def test_compiler_catalog_fetch_and_event_delivery_survive_registration_race(self):
        compiler = ThreadingHTTPServer(("127.0.0.1", 0), CompilerHandler)
        compiler.received, compiler.lock, compiler.reject_first_event = [], threading.Lock(), True
        compiler.package = package(self.gateway.catalog.revision)
        thread = threading.Thread(target=compiler.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        upstream = Upstream("http://127.0.0.1:" + str(compiler.server_port))
        self.gateway.upstream = upstream
        self.gateway.delivery.upstream = upstream
        try:
            validation = self.gateway.preflight({"run_id": "run-1"}, "pre")
            result = self.gateway.submit({"package": validation["package"], "validation_token": validation["validation_token"]}, "d", True)
            self.finish(result["execution_id"])
            for _ in range(30):
                self.gateway.delivery.pump()
                if not self.gateway.store.delivery_status()["pending"]:
                    break
            self.assertEqual(self.gateway.store.delivery_status()["pending"], 0)
            paths = [p for p, _ in compiler.received]
            self.assertIn("/v1/integrations/catalog/snapshots", paths)
            events = [b for p, b in compiler.received if "/executions/" in p]
            self.assertEqual(events[0]["event_id"], events[1]["event_id"])
            unique = {e["event_id"]: e for e in events}
            self.assertEqual([e["sequence"] for e in unique.values()], list(range(1, len(unique) + 1)))
            self.assertTrue(all(e["execution_id"] == result["execution_id"] for e in events))
        finally:
            compiler.shutdown()
            compiler.server_close()
            thread.join()


class RegistryAndDriverTests(unittest.TestCase):
    def test_restart_does_not_claim_a_lost_preflight_reservation(self):
        with TemporaryDirectory() as temp:
            scene = load_scene_config(SCENE)
            gateway = Gateway(scene, temp)
            graph = package(gateway.catalog.revision)
            gateway.preflight({"package": graph}, "pre")
            gateway.close()
            restarted = Gateway(scene, temp)
            try:
                with self.assertRaises(ContractError) as caught:
                    restarted.preflight({"package": graph}, "pre")
                self.assertEqual(caught.exception.body["code"], "RESERVATION_EXPIRED")
                self.assertTrue(restarted.preflight({"package": graph}, "new-pre")["accepted"])
            finally:
                restarted.close()

    def test_invalid_ack_and_recursive_response_remain_retryable_in_outbox(self):
        class FakeUpstream:
            response = []
            def request(self, *_):
                if isinstance(self.response, Exception):
                    raise self.response
                return self.response
        with TemporaryDirectory() as temp:
            store = Store(temp)
            upstream = FakeUpstream()
            delivery = DeliveryWorker(store, upstream)
            store.enqueue("test", "/events", {"event_id": "same-id"})
            try:
                for response in ([], {"accepted": "true"}, {"accepted": False}, {"diagnostics": [{"severity": "blocking"}]}, RecursionError(), BadStatusLine("bad"), IncompleteRead(b"")):
                    upstream.response = response
                    self.assertEqual(delivery.pump(), 0)
                    self.assertEqual(store.delivery_status()["pending"], 1)
                    self.assertEqual(store.pending_deliveries()[0]["id"], "same-id")
                upstream.response = {"accepted": True}
                self.assertEqual(delivery.pump(), 1)
                self.assertEqual(store.delivery_status()["pending"], 0)
            finally:
                store.close()

    def test_onsite_reset_requires_free_owners_and_retains_history(self):
        with TemporaryDirectory() as temp:
            scene = load_scene_config(SCENE)
            directory, owner = Path(temp) / "db", Path(temp) / "owner"
            gateway = Gateway(scene, directory)
            graph = package(gateway.catalog.revision)
            result = gateway.submit({"graph": graph}, "key")
            gateway.worker.join()
            with self.assertRaises(RuntimeError):
                reset_scene(directory, scene, "mock", owner)
            gateway.close()
            lease = DeviceLease(owner, "legacy-control").acquire()
            with self.assertRaises(RuntimeError):
                reset_scene(directory, scene, "mock", owner)
            lease.release()
            self.assertTrue(lease.scene_marker.exists())
            reset_scene(directory, scene, "mock", owner)
            self.assertTrue(lease.scene_marker.exists())
            restarted = Gateway(scene, directory)
            try:
                self.assertEqual(restarted.store.object_state()["left"]["location"], "object:A")
                self.assertEqual(restarted.submit({"graph": graph}, "key")["execution_id"], result["execution_id"])
            finally:
                restarted.close()

    def test_registry_mode_is_fixed_and_scene_stamp_invalidates_other_directories(self):
        with TemporaryDirectory() as temp:
            scene = load_scene_config(SCENE)
            owner = Path(temp) / "owner"
            first, second = Path(temp) / "live-1", Path(temp) / "live-2"
            reset_scene(first, scene, "live", owner)
            with self.assertRaises(ValueError):
                Gateway(scene, first, mode="mock")
            def check_gate(directory):
                gateway = object.__new__(Gateway)
                gateway.mode, gateway.lease_path, gateway.lease = "live", owner, None
                gateway.store, gateway.catalog = Store(directory), Catalog(scene, "live")
                try:
                    gateway._acquire_lease()
                finally:
                    gateway._release_lease()
                    gateway.store.close()
            check_gate(first)
            reset_scene(second, scene, "live", owner)
            check_gate(second)
            with self.assertRaises(ContractError) as caught:
                check_gate(first)
            self.assertEqual(caught.exception.body["code"], "SCENE_RESET_REQUIRED")
            lease = DeviceLease(owner, "legacy-control").acquire()
            lease.release()
            with self.assertRaises(ContractError):
                check_gate(second)
            reset_scene(second, scene, "live", owner)
            check_gate(second)

    def test_cancel_gate_prevents_late_gripper_command_after_stop(self):
        entered, resume, calls, errors = threading.Event(), threading.Event(), [], []
        class Dashboard:
            def StopScript(self):
                return "0,{},StopScript();"
            def RobotMode(self):
                return "0,{5},RobotMode();"
        class Gripper:
            def get_current_position(self):
                return 127
            def move(self, target, speed, force):
                calls.append(target)
                return 0
        class Adapter(DobotHardwareAdapter):
            def _gripper_position(self, arm):
                # Pause before the I/O gate, reproducing a stale pre-command check.
                if threading.current_thread().name == "blocked-skill":
                    entered.set()
                    if not resume.wait(2):
                        raise TimeoutError("Test did not resume skill")
                return super()._gripper_position(arm)
        adapter = Adapter.from_components({"left": Dashboard()}, {}, {"left": Gripper()})
        cancel = threading.Event()
        adapter.set_cancel_event(cancel)
        def skill():
            try:
                adapter.close_gripper("left")
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=skill, name="blocked-skill")
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            cancel.set()
            self.assertTrue(adapter.stop_and_verify()["confirmed"])
        finally:
            resume.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [127])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], InterruptedError)

    def test_device_lease_is_shared_and_owner_checked(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "owner"
            first = DeviceLease(path).acquire()
            with self.assertRaises(RuntimeError):
                DeviceLease(path).acquire()
            first.release()
            with DeviceLease(path):
                self.assertTrue(path.exists())

    def test_process_guard_prevents_a_second_registry_owner(self):
        with TemporaryDirectory() as temp:
            store = Store(temp)
            try:
                with self.assertRaises(RuntimeError):
                    Store(temp)
            finally:
                store.close()

    def test_restart_retains_completed_idempotency_and_interrupts_unfinished_intent(self):
        with TemporaryDirectory() as temp:
            scene = load_scene_config(SCENE)
            gateway = Gateway(scene, temp)
            graph = package(gateway.catalog.revision)
            completed = gateway.submit({"graph": graph}, "key")
            gateway.worker.join()
            graph2 = package(gateway.catalog.revision, "2")
            interrupted = gateway.store.create("crash-key", gateway.catalog.validate(graph2))
            gateway.close()
            restarted = Gateway(scene, temp)
            try:
                self.assertEqual(restarted.submit({"graph": graph}, "key")["execution_id"], completed["execution_id"])
                self.assertEqual(restarted.status(interrupted["id"])["status"], "interrupted")
                self.assertEqual(restarted.submit({"graph": graph2}, "crash-key")["status"], "interrupted")
                self.assertIsNone(restarted.worker)
            finally:
                restarted.close()

    def test_driver_wait_is_cancellable_without_hardware(self):
        adapter = DobotHardwareAdapter.from_components({}, {}, {})
        cancel = threading.Event()
        adapter.set_cancel_event(cancel)
        timer = threading.Timer(0.02, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(InterruptedError):
                adapter.execute_atomic(AtomicNode("wait", 0, "wait", "left", parameters={"seconds": 10}), None)
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 1)

    def test_stop_confirmation_is_independent_of_cancel_flag(self):
        class Dashboard:
            def StopScript(self):
                return "0,{},StopScript();"
            def RobotMode(self):
                return "0,{5},RobotMode();"
        adapter = DobotHardwareAdapter.from_components({"left": Dashboard()}, {}, {})
        cancel = threading.Event()
        cancel.set()
        adapter.set_cancel_event(cancel)
        self.assertTrue(adapter.stop_and_verify()["confirmed"])

    def test_stop_attempts_other_arm_when_one_fails_and_holds_grippers(self):
        stopped, held = [], []
        class Dashboard:
            def __init__(self, arm):
                self.arm = arm
            def StopScript(self):
                stopped.append(self.arm)
                if self.arm == "left":
                    raise OSError("Disconnected")
                return "0,{},StopScript();"
            def RobotMode(self):
                return "0,{5},RobotMode();"
        class Gripper:
            def get_current_position(self):
                return 127
            def move(self, target, speed, force):
                held.append(target)
                return 0
        adapter = DobotHardwareAdapter.from_components(
            {arm: Dashboard(arm) for arm in ("left", "right")}, {},
            {arm: Gripper() for arm in ("left", "right")})
        result = adapter.stop_and_verify()
        self.assertFalse(result["confirmed"])
        self.assertEqual(set(stopped), {"left", "right"})
        self.assertEqual(held, [127, 127])
        self.assertTrue(result["arms"]["right"]["stop_confirmed"])

    def test_live_mode_cannot_be_enabled_by_default(self):
        with TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                Gateway(load_scene_config(SCENE), temp, mode="live")


if __name__ == "__main__":
    unittest.main()
