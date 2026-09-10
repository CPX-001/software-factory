"""Transactional discovery state, bounded snapshots and a deterministic readiness gate."""

import json

from .discovery_contract import CRITERIA, validate
from .workflow import Store, WorkflowError

MAX_CONTEXT_BYTES = 60_000
MAX_ACTIVE_ITEMS = 100
MAX_MESSAGE_CHARS = 8_000
MAX_MESSAGE_BYTES = 16_000


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def migrate(db):
    """Add discovery to v1 stores without altering existing workflow or audit records."""
    previous_version = db.execute("PRAGMA user_version").fetchone()[0]
    statements = (
        """CREATE TABLE IF NOT EXISTS discovery_items (
            key TEXT PRIMARY KEY, data TEXT NOT NULL, turn_id INTEGER)""",
        """CREATE TABLE IF NOT EXISTS discovery_questions (
            key TEXT PRIMARY KEY, data TEXT NOT NULL, resolution TEXT, turn_id INTEGER)""",
        """CREATE TABLE IF NOT EXISTS discovery_decisions (
            key TEXT PRIMARY KEY, decision_id INTEGER NOT NULL UNIQUE REFERENCES decisions(id),
            data TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS discovery_turns (
            id INTEGER PRIMARY KEY, message TEXT NOT NULL, response TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            completed_at TEXT)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS one_pending_discovery_turn
            ON discovery_turns(status) WHERE status = 'pending'""",
        """CREATE TABLE IF NOT EXISTS discovery_meta (
            id INTEGER PRIMARY KEY CHECK(id = 1), assessment TEXT, readiness TEXT,
            completed_at TEXT)""",
        "INSERT OR IGNORE INTO discovery_meta(id) VALUES (1)",
    )
    for statement in statements:
        db.execute(statement)
    if previous_version < 2:
        db.execute("PRAGMA user_version = 2")
    if previous_version == 1 and db.execute("SELECT phase FROM workflow WHERE id = 1").fetchone()[0] == "discovery":
        for decision in db.execute("SELECT id, answer FROM decisions WHERE answer IS NOT NULL").fetchall():
            remember_decision(db, decision["id"], decision["answer"])


def read_state(db):
    result = {"knowledge": [], "questions": [], "decision_details": [], "assessment": None,
              "readiness": None, "completed_at": None, "pending_turn": None, "last_message": None}
    if db.execute("PRAGMA user_version").fetchone()[0] < 2:
        return result
    items = [json.loads(row["data"]) for row in db.execute("SELECT data FROM discovery_items ORDER BY key")]
    result["knowledge"] = [item for item in items if item["status"] != "superseded"]
    result["questions"] = [json.loads(row["data"]) for row in db.execute(
        "SELECT data FROM discovery_questions WHERE resolution IS NULL ORDER BY key")]
    result["decision_details"] = [dict(json.loads(row["data"]), id=row["decision_id"])
                                  for row in db.execute("""SELECT dd.* FROM discovery_decisions dd
                                  JOIN decisions d ON d.id = dd.decision_id WHERE d.answer IS NULL
                                  ORDER BY dd.decision_id""")]
    meta = db.execute("SELECT * FROM discovery_meta WHERE id = 1").fetchone()
    if meta:
        result.update({key: json.loads(meta[key]) if meta[key] else None
                       for key in ("assessment", "readiness")})
        result["completed_at"] = meta["completed_at"]
    pending = db.execute("SELECT id FROM discovery_turns WHERE status = 'pending'").fetchone()
    result["pending_turn"] = pending["id"] if pending else None
    last = db.execute("SELECT response FROM discovery_turns WHERE status = 'completed' ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        result["last_message"] = json.loads(last["response"])["message"]
    return result


def remember_decision(db, decision_id, answer):
    question = db.execute("SELECT question FROM decisions WHERE id = ?", (decision_id,)).fetchone()[0]
    item = {"key": f"human_decision_{decision_id}", "category": "decisions",
            "text": f"{question}\nRespuesta: {answer}", "status": "known", "blocking": False,
            "basis": f"Human decision {decision_id}, recorded directly by the application"}
    db.execute("INSERT OR REPLACE INTO discovery_items(key, data) VALUES (?, ?)",
               (item["key"], dumps(item)))
    # External answers invalidate the previous assessment until incorporated in a model turn.
    db.execute("UPDATE discovery_meta SET assessment = NULL, readiness = NULL WHERE id = 1")


def evaluate_readiness(knowledge, questions, decisions, assessment):
    """The model supplies semantic evidence; code verifies coverage, certainty and blockers."""
    items = {item["key"]: item for item in knowledge}
    evidence = {entry["criterion"]: entry["keys"] for entry in assessment["evidence"]} if assessment else {}
    missing = []
    for criterion, certainty in CRITERIA.items():
        keys = evidence.get(criterion, [])
        if not keys or not all(key in items and items[key]["category"] == criterion
                               and items[key]["status"] in certainty and not items[key]["blocking"]
                               for key in keys):
            missing.append(criterion)
    blockers = [item["key"] for item in knowledge
                if item["status"] == "conflict" or item["blocking"]]
    pending = [d["id"] for d in decisions if d["answer"] is None]
    return {"ready": bool(assessment and assessment["ready"] and not missing and not blockers
                           and not pending and not questions),
            "missing_criteria": missing, "blocking_items": blockers,
            "pending_decisions": pending, "pending_questions": [q["key"] for q in questions],
            "model_ready": bool(assessment and assessment["ready"])}


def model_context(snapshot, message):
    """Never read or serialize turns/events or a resumed SDK conversation here."""
    discovery = snapshot["discovery"]
    context = {"knowledge": discovery["knowledge"], "pending_questions": discovery["questions"],
               "pending_decisions": [{"id": d["id"], "question": d["question"]}
                                     for d in snapshot["decisions"] if d["answer"] is None],
               "decision_details": discovery["decision_details"],
               "previous_readiness": discovery["readiness"],
               "readiness_policy": {key: list(value) for key, value in CRITERIA.items()},
               "user_message": message}
    encoded = dumps(context)
    if len(discovery["knowledge"]) > MAX_ACTIVE_ITEMS or len(encoded.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise WorkflowError("Discovery snapshot exceeds its context budget; consolidate active knowledge before retrying. No facts were truncated.")
    return context


class Discovery:
    def __init__(self, store: Store, model):
        self.store = store
        self.model = model

    def submit(self, message):
        self.enqueue(message)
        return self.resume()

    def enqueue(self, message, request_id=None):
        if (not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS
                or len(dumps(message).encode("utf-8")) > MAX_MESSAGE_BYTES):
            raise WorkflowError(f"Message must contain 1..{MAX_MESSAGE_CHARS} characters, at most {MAX_MESSAGE_BYTES} UTF-8 bytes as JSON")
        # Save input before calling Codex; a failed/interrupted call can be resumed.
        with self.store._connection(write=True) as db:
            if request_id is not None:
                old = db.execute('SELECT message,turn_id FROM factory_requests WHERE id=?', (request_id,)).fetchone()
                if old:
                    if old['message'] != message:
                        raise WorkflowError('Request ID already used for different input')
                    return old['turn_id']
            state = db.execute("SELECT phase, revision FROM workflow WHERE id = 1").fetchone()
            if state["phase"] != "discovery":
                raise WorkflowError("This project is no longer in discovery")
            if db.execute("SELECT 1 FROM discovery_turns WHERE status = 'pending'").fetchone():
                raise WorkflowError("A discovery message is pending; retry it before sending another")
            turn_id = db.execute("INSERT INTO discovery_turns(message, status) VALUES (?, 'pending')", (message,)).lastrowid
            if request_id is not None:
                db.execute('INSERT INTO factory_requests VALUES (?, ?, ?)', (request_id, message, turn_id))
            self.store._record(db, state["revision"] + 1, "discovery_input", {"turn_id": turn_id})
        return turn_id

    def resume(self):
        # Snapshot and pending input must belong to the same transaction/revision.
        with self.store._connection() as db:
            state = dict(db.execute("SELECT phase, revision FROM workflow WHERE id = 1").fetchone())
            if state["phase"] != "discovery":
                raise WorkflowError("This project is no longer in discovery")
            turn = db.execute("SELECT * FROM discovery_turns WHERE status = 'pending'").fetchone()
            if turn is None:
                raise WorkflowError("There is no pending discovery message")
            turn_id, message = turn["id"], turn["message"]
            state["decisions"] = [dict(row) for row in db.execute("SELECT * FROM decisions ORDER BY id")]
            state["discovery"] = read_state(db)
        response = self.model.respond(model_context(state, message))
        validate(response)
        return self._apply(turn_id, message, state["revision"], response)

    def _apply(self, turn_id, message, revision, response):
        with self.store._connection(write=True) as db:
            if self.store._check(db, revision) != "discovery":
                raise WorkflowError("This project is no longer in discovery")
            turn = db.execute("SELECT status FROM discovery_turns WHERE id = ?", (turn_id,)).fetchone()
            if not turn or turn[0] != "pending":
                raise WorkflowError("Discovery turn is no longer pending")
            self._validate_operations(response)
            for item in response["knowledge"]:
                if item["key"].startswith("human_decision_"):
                    raise WorkflowError("Human decision records are managed by the application")
                old = db.execute("SELECT data FROM discovery_items WHERE key = ?", (item["key"],)).fetchone()
                if old and json.loads(old[0])["category"] != item["category"]:
                    raise WorkflowError("An existing knowledge key cannot change category")
                if item["status"] == "superseded" and not old:
                    raise WorkflowError("Cannot supersede nonexistent knowledge")
                db.execute("""INSERT INTO discovery_items VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET data = excluded.data, turn_id = excluded.turn_id""",
                           (item["key"], dumps(item), turn_id))
            for resolution in response["resolve_questions"]:
                changed = db.execute("UPDATE discovery_questions SET resolution = ? WHERE key = ? AND resolution IS NULL",
                                     (resolution["reason"], resolution["key"])).rowcount
                if not changed:
                    raise WorkflowError("Cannot resolve a question that is not pending")
            for question in response["questions"]:
                db.execute("""INSERT INTO discovery_questions VALUES (?, ?, NULL, ?)
                    ON CONFLICT(key) DO UPDATE SET data = excluded.data, resolution = NULL, turn_id = excluded.turn_id""",
                           (question["key"], dumps(question), turn_id))
            for answer in response["decision_answers"]:
                if answer["quote"] not in message:
                    raise WorkflowError("A human decision requires an exact quote from the latest user message")
                changed = db.execute("""UPDATE decisions SET answer = ?,
                    answered_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ? AND answer IS NULL""",
                                     (answer["quote"], answer["id"])).rowcount
                if not changed:
                    raise WorkflowError("Cannot answer a decision that is not pending")
                remember_decision(db, answer["id"], answer["quote"])
            for decision in response["decisions"]:
                previous = db.execute("SELECT decision_id FROM discovery_decisions WHERE key = ?", (decision["key"],)).fetchone()
                if previous:
                    raise WorkflowError("Decision key already exists; answer the existing choice or use a new key")
                decision_id = db.execute("INSERT INTO decisions(question) VALUES (?)", (decision["question"],)).lastrowid
                db.execute("INSERT INTO discovery_decisions VALUES (?, ?, ?)",
                           (decision["key"], decision_id, dumps(decision)))
            discovery = read_state(db)
            decisions = [dict(row) for row in db.execute("SELECT * FROM decisions ORDER BY id")]
            keys = {item["key"] for item in discovery["knowledge"]}
            for evidence in response["assessment"]["evidence"]:
                if not set(evidence["keys"]) <= keys:
                    raise WorkflowError("Assessment references missing or superseded knowledge")
            if len(discovery["questions"]) + sum(d["answer"] is None for d in decisions) > 12:
                raise WorkflowError("Too many pending questions/decisions; resolve existing ones first")
            readiness = evaluate_readiness(discovery["knowledge"], discovery["questions"], decisions, response["assessment"])
            # Refuse an unusable response that leaves the CLI with no way to continue.
            if not readiness["ready"] and not discovery["questions"] and not readiness["pending_decisions"]:
                raise WorkflowError("Incomplete discovery must provide a useful question or human decision")
            discovery["readiness"] = readiness
            # Reserve room for the next input; never commit a snapshot too large to resume.
            model_context({"discovery": discovery, "decisions": decisions}, "x" * MAX_MESSAGE_BYTES)
            db.execute("UPDATE discovery_meta SET assessment = ?, readiness = ? WHERE id = 1",
                       (dumps(response["assessment"]), dumps(readiness)))
            db.execute("""UPDATE discovery_turns SET status = 'completed', response = ?,
                completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?""", (dumps(response), turn_id))
            self.store._record(db, revision + 1, "discovery_updated",
                               {"turn_id": turn_id, "readiness": readiness})
            if readiness["ready"]:
                db.execute("UPDATE discovery_meta SET completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = 1")
                db.execute("UPDATE workflow SET phase = 'architecture' WHERE id = 1")
                self.store._record(db, revision + 2, "phase_changed",
                                   {"from": "discovery", "to": "architecture", "turn_id": turn_id,
                                    "gate": readiness})
        return self.store.snapshot()

    @staticmethod
    def _validate_operations(response):
        for name, key in (("knowledge", "key"), ("questions", "key"), ("resolve_questions", "key"),
                          ("decisions", "key"), ("decision_answers", "id")):
            values = [item[key] for item in response[name]]
            if len(values) != len(set(values)):
                raise WorkflowError(f"Duplicate {name} operations")
        criteria = [entry["criterion"] for entry in response["assessment"]["evidence"]]
        if len(criteria) != len(set(criteria)):
            raise WorkflowError("Duplicate readiness criteria")
        if len(response["questions"]) + len(response["decisions"]) > 3:
            raise WorkflowError("Ask at most three questions/decisions per interaction")
        if {q["key"] for q in response["questions"]} & {q["key"] for q in response["resolve_questions"]}:
            raise WorkflowError("Cannot ask and resolve the same question in one turn")
        for item in response["knowledge"]:
            if item["status"] == "conflict" and not item["blocking"]:
                raise WorkflowError("Conflicts must block readiness")
            if item["status"] == "superseded" and item["blocking"]:
                raise WorkflowError("Superseded knowledge cannot be blocking")
