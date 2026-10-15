"""Transactional local execution registry and acknowledged outbound event queue."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ..graph import utc_now
from .contracts import ContractError, DEVICES, SKILLS, canonical


TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


class ProcessGuard:
    """OS-released lock: a second gateway cannot recover the first one's live DB."""
    def __init__(self, path):
        self.file = Path(path).open("a+b")
        try:
            import os
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise RuntimeError("Another gateway owns this data directory") from error

    def close(self):
        self.file.close()


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.guard = ProcessGuard(directory / "gateway.lock")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(directory / "gateway.sqlite3"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS executions(
                id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, digest TEXT NOT NULL,
                package TEXT NOT NULL, status TEXT NOT NULL, accepted_at TEXT NOT NULL,
                node_statuses TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS reservations(
                token TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, digest TEXT NOT NULL,
                expires REAL NOT NULL, response TEXT NOT NULL, execution_id TEXT);
            CREATE TABLE IF NOT EXISTS invocations(
                key TEXT PRIMARY KEY, execution_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                execution_id TEXT NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(execution_id, sequence));
            CREATE TABLE IF NOT EXISTS outbox(
                id TEXT PRIMARY KEY, stream TEXT NOT NULL, path TEXT NOT NULL, body TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT);
            CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata(name TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            with self.db:
                yield self.db

    def close(self):
        with self.lock:
            self.db.close()
            self.guard.close()

    def bind_mode(self, mode):
        with self.transaction() as db:
            row = db.execute("SELECT value FROM metadata WHERE name='mode'").fetchone()
            if row and row[0] != mode:
                raise ValueError("A gateway data directory is permanently bound to its mode; use a separate directory")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('mode',?)", (mode,))
            db.execute("INSERT OR IGNORE INTO metadata VALUES('registry_id',?)", (uuid4().hex,))

    def scene_identity(self):
        with self.lock:
            return dict(self.db.execute("SELECT name,value FROM metadata WHERE name IN ('registry_id','scene_epoch','verified_scene_revision')"))

    def get(self, execution_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        if row is None:
            raise ContractError("EXECUTION_NOT_FOUND", "Unknown execution_id", status=404)
        return self._execution(row)

    def by_key(self, key):
        with self.lock:
            row = self.db.execute("SELECT * FROM executions WHERE key=?", (key,)).fetchone()
        return self._execution(row) if row else None

    @staticmethod
    def _execution(row):
        value = dict(row)
        for field in ("package", "node_statuses", "error"):
            if value[field] is not None:
                value[field] = json.loads(value[field])
        return value

    def create(self, key, plan, token=None):
        execution_id = "execution-" + uuid4().hex
        nodes = {node_id: "pending" for node_id in plan.order}
        invocation_keys = [n["skill_invocation"]["idempotency_key"] for n in plan.package["nodes"] if n["node_type"] == "action"]
        try:
            with self.transaction() as db:
                for invocation in invocation_keys:
                    if db.execute("SELECT 1 FROM invocations WHERE key=?", (invocation,)).fetchone():
                        raise ContractError("IDEMPOTENCY_CONFLICT", "Invocation key already belongs to another execution", status=409)
                db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,?,NULL)",
                           (execution_id, key, plan.fingerprint, canonical(plan.package), "accepted", utc_now(), canonical(nodes)))
                db.executemany("INSERT INTO invocations VALUES(?,?)", [(k, execution_id) for k in invocation_keys])
                if token:
                    db.execute("UPDATE reservations SET execution_id=? WHERE token=?", (execution_id, token))
        except sqlite3.IntegrityError as error:
            raise ContractError("IDEMPOTENCY_CONFLICT", "Request key is already registered", status=409) from error
        return self.get(execution_id)

    def reservation_by_key(self, key):
        with self.lock:
            row = self.db.execute("SELECT * FROM reservations WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def reservation(self, token):
        with self.lock:
            row = self.db.execute("SELECT * FROM reservations WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None

    def reserve(self, key, plan, expires, response):
        with self.transaction() as db:
            db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,NULL)",
                       (response["validation_token"], key, plan.fingerprint, expires, canonical(response)))

    def recover(self, live):
        with self.lock:
            active = [row[0] for row in self.db.execute(
                "SELECT id FROM executions WHERE status NOT IN ('succeeded','failed','cancelled','interrupted')")]
        for execution_id in active:
            self.event(execution_id, "execution_interrupted", payload={"reason": "gateway_restarted"},
                       status="interrupted", error={"code": "EXECUTION_INTERRUPTED", "retryable": False},
                       block_unfinished=True)
        if active and live:
            self.quarantine("Previous live execution requires onsite state verification")

    def quarantine(self, reason):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES('quarantine',?)", (reason,))

    def quarantine_reason(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE name='quarantine'").fetchone()
        return row[0] if row else None

    def verified_scene_revision(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE name='verified_scene_revision'").fetchone()
        return row[0] if row else None

    def reset_scene(self, revision):
        with self.transaction() as db:
            db.execute("DELETE FROM metadata WHERE name IN ('quarantine','object_state')")
            db.execute("INSERT OR REPLACE INTO metadata VALUES('verified_scene_revision',?)", (revision,))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('scene_epoch',?)", (uuid4().hex,))

    def object_state(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE name='object_state'").fetchone()
        return json.loads(row[0]) if row else {"left": {"held": False, "location": "object:A"},
                                               "right": {"held": False, "location": "object:B"}}

    @staticmethod
    def _enqueue(db, stream, path, body, item_id=None):
        item_id = item_id or body.get("event_id") or uuid4().hex
        db.execute("INSERT OR IGNORE INTO outbox(id,stream,path,body) VALUES(?,?,?,?)",
                   (item_id, stream, path, canonical(body)))

    def enqueue(self, stream, path, body, item_id=None):
        with self.transaction() as db:
            self._enqueue(db, stream, path, body, item_id)

    def event(self, execution_id, event_type, node_id=None, payload=None, status=None,
              node_status=None, error=None, block_unfinished=False):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
            package = json.loads(row["package"])
            nodes = json.loads(row["node_statuses"])
            if node_id and node_status:
                nodes[node_id] = node_status
            if block_unfinished:
                unfinished_running = "interrupted" if status == "interrupted" else "cancelled"
                nodes = {k: ("blocked" if v == "pending" else unfinished_running if v == "running" else v) for k, v in nodes.items()}
            db.execute("UPDATE executions SET status=?,node_statuses=?,error=? WHERE id=?",
                       (status or row["status"], canonical(nodes), canonical(error) if error else row["error"], execution_id))
            sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE execution_id=?", (execution_id,)).fetchone()[0]
            body = {"event_id": "event-" + uuid4().hex, "execution_id": execution_id,
                    "graph_id": package["package_id"], "sequence": sequence, "event_type": event_type,
                    "payload": payload or {}, "occurred_at": utc_now()}
            if node_id:
                body["node_id"] = node_id
                node = next(n for n in package["nodes"] if n["node_id"] == node_id)
                if node["node_type"] == "action":
                    call = node["skill_invocation"]
                    body.update({k: call[k] for k in ("device_id", "skill_id", "skill_version")})
                    # Only the embodied family carries the registered object model. The
                    # dobot.* atoms move a tool to a taught pose and have no
                    # destination_ref, so applying held/location bookkeeping to them
                    # would both raise and invent state that no sensor confirmed.
                    if node_status == "succeeded" and call["skill_id"] in SKILLS:
                        state = self.object_state()
                        arm = DEVICES[call["device_id"]]
                        if call["skill_id"] == "embodied.pick":
                            state[arm]["held"] = True
                        else:
                            state[arm] = {"held": False, "location": call["parameters"]["destination_ref"]}
                        db.execute("INSERT OR REPLACE INTO metadata VALUES('object_state',?)", (canonical(state),))
            if error:
                body.update(error_code=error["code"], retryable=error.get("retryable", False))
            db.execute("INSERT INTO events VALUES(?,?,?)", (execution_id, sequence, canonical(body)))
            self._enqueue(db, execution_id, "/v1/executions/" + execution_id + "/events", body)
        return body

    def device_event(self, device_id, state, execution_id=None, mode="mock"):
        with self.transaction() as db:
            counter = "device:" + device_id
            row = db.execute("SELECT value FROM counters WHERE name=?", (counter,)).fetchone()
            sequence = (row[0] if row else 0) + 1
            db.execute("INSERT OR REPLACE INTO counters VALUES(?,?)", (counter, sequence))
            body = {"event_id": "device-event-" + uuid4().hex, "device_id": device_id,
                    "capability_version": "1.0.0", "sequence": sequence, "occurred_at": utc_now(),
                    "state": state, "health": "healthy" if mode == "mock" else "degraded",
                    "mode": mode, "hardware_connected": mode == "live" and state == "running"}
            if execution_id:
                body["active_execution_id"] = execution_id
            self._enqueue(db, "device:" + device_id, "/v1/integrations/device-state-events", body)

    def events(self, execution_id, after=0, limit=100):
        self.get(execution_id)
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                "SELECT body FROM events WHERE execution_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (execution_id, after, limit))]

    def pending_deliveries(self, limit=100):
        with self.lock:
            # Only the first unacknowledged item per stream is eligible, preserving sequence.
            return [dict(row) for row in self.db.execute("""
                SELECT rowid,* FROM outbox o WHERE acknowledged=0 AND NOT EXISTS(
                    SELECT 1 FROM outbox earlier WHERE earlier.stream=o.stream
                    AND earlier.acknowledged=0 AND earlier.rowid<o.rowid)
                ORDER BY rowid LIMIT ?""", (limit,))]

    def delivery_result(self, item_id, success, error=None):
        with self.transaction() as db:
            db.execute("UPDATE outbox SET acknowledged=?,attempts=attempts+1,last_error=? WHERE id=?",
                       (int(success), error, item_id))

    def delivery_status(self):
        with self.lock:
            rows = self.db.execute("SELECT acknowledged,COUNT(*) FROM outbox GROUP BY acknowledged").fetchall()
            errors = [dict(r) for r in self.db.execute(
                "SELECT id,path,attempts,last_error FROM outbox WHERE acknowledged=0 AND last_error IS NOT NULL LIMIT 10")]
        return {"acknowledged": dict(rows).get(1, 0), "pending": dict(rows).get(0, 0), "errors": errors}
