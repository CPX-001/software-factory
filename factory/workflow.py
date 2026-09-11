"""One database per project; every mutation and its event commit together."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


TRANSITIONS = {
    "discovery": {"architecture"},
    "requirements": {"architecture"},
    "architecture": {"planning"},
    "planning": {"execution"},
    "execution": {"verification"},
    "verification": {"execution", "completed"},
    "completed": set(),
}


class WorkflowError(ValueError):
    """Invalid workflow operation or unsupported persistent state."""


def next_action(snapshot):
    """Pure recommendation; consulting it never invokes a phase handler."""
    pending = [d["id"] for d in snapshot["decisions"] if d["answer"] is None]
    if pending:
        return {"action": "wait_for_human", "decision_ids": pending}
    if snapshot["phase"] == "completed":
        return {"action": "done"}
    if snapshot["phase"] == "discovery":
        discovery = snapshot.get("discovery", {})
        return {"action": "discovery", "implemented": True,
                "pending_questions": discovery.get("questions", []),
                "retry_pending_turn": discovery.get("pending_turn") is not None,
                "readiness": discovery.get("readiness")}
    if snapshot["phase"] == "architecture":
        return {"action": "architecture", "implemented": True,
                "stage": snapshot.get("architecture", {}).get("stage", "not_started"),
                "blockers": snapshot.get("architecture", {}).get("blockers", [])}
    if snapshot["phase"] == "planning":
        return {"action": "planning", "implemented": True,
                "stage": snapshot.get("planning", {}).get("stage", "not_started"),
                "blockers": snapshot.get("planning", {}).get("blockers", [])}
    return {"action": snapshot["phase"], "implemented": False}


class Store:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.path = self.project / ".factory" / "state.sqlite3"

    @contextmanager
    def _connection(self, *, initialize=False, write=False):
        if initialize:
            if not self.project.is_dir():
                raise WorkflowError("Project must be an existing directory")
            self.path.parent.mkdir(exist_ok=True)
        elif not self.path.is_file():
            raise WorkflowError("Factory is not initialized; run init first")
        mode = "rwc" if initialize else "rw" if write else "ro"
        db = sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA synchronous = FULL")
            db.execute("BEGIN IMMEDIATE" if write or initialize else "BEGIN")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (1, 2, 3, 4, 5, 6, 7, 8, 9) and not (initialize and version == 0):
                raise WorkflowError(f"Unsupported schema version: {version}")
            if version == 1 and (write or initialize):
                from .discovery import migrate
                migrate(db)
            if version in (1, 2) and (write or initialize):
                from .architecture import migrate
                migrate(db)
            if version in (1, 2, 3) and (write or initialize):
                from .runtime import migrate
                migrate(db)
            if version in (1, 2, 3, 4) and (write or initialize):
                from .planning import migrate
                migrate(db)
            if version in (1, 2, 3, 4, 5) and (write or initialize):
                from .execution_store import migrate
                migrate(db)
            if version in (1, 2, 3, 4, 5, 6) and (write or initialize):
                from .continuation_store import migrate
                migrate(db)
            if version in (1, 2, 3, 4, 5, 6, 7) and (write or initialize):
                from .milestone_store import migrate
                migrate(db)
            if version in range(1, 9) and (write or initialize):
                from .project_store import migrate
                migrate(db)
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        with self._connection(initialize=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS workflow (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                phase TEXT NOT NULL, revision INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY, question TEXT NOT NULL, answer TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                answered_at TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, revision INTEGER NOT NULL UNIQUE,
                kind TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""")
            if db.execute("SELECT 1 FROM workflow").fetchone() is None:
                db.execute("INSERT INTO workflow VALUES (1, 'discovery', 0)")
                self._event(db, 0, "initialized", {"phase": "discovery"})
            from .discovery import migrate
            migrate(db)
            from .architecture import migrate
            migrate(db)
            from .runtime import migrate
            migrate(db)
            from .planning import migrate
            migrate(db)
            from .execution_store import migrate
            migrate(db)
            from .continuation_store import migrate
            migrate(db)
            from .milestone_store import migrate
            migrate(db)
            from .project_store import migrate
            migrate(db)

    @staticmethod
    def _event(db, revision, kind, payload):
        db.execute("INSERT INTO events(revision, kind, payload) VALUES (?, ?, ?)",
                   (revision, kind, json.dumps(payload, ensure_ascii=False)))

    def snapshot(self):
        with self._connection() as db:
            state = dict(db.execute("SELECT phase, revision FROM workflow WHERE id = 1").fetchone())
            state["decisions"] = [dict(row) for row in db.execute("SELECT * FROM decisions ORDER BY id")]
            from .discovery import evaluate_readiness, read_state
            state["discovery"] = read_state(db)
            from .architecture import read_state as architecture_state
            state["architecture"] = architecture_state(db)
            from .planning import read_state as planning_state
            state["planning"] = planning_state(db)
            discovery = state["discovery"]
            if state["phase"] == "discovery" and discovery["assessment"]:
                discovery["readiness"] = evaluate_readiness(
                    discovery["knowledge"], discovery["questions"], state["decisions"], discovery["assessment"])
            state["status"] = ("waiting_for_human" if any(d["answer"] is None for d in state["decisions"])
                               else "blocked" if (state["architecture"]["blockers"] or state["planning"]["blockers"])
                               else "input_pending" if discovery["pending_turn"] is not None
                               else "waiting_for_input" if state["phase"] == "discovery" and discovery["questions"]
                               else "completed" if state["phase"] == "completed" else "ready")
            return state

    def events(self):
        with self._connection() as db:
            return [dict(row, payload=json.loads(row["payload"]))
                    for row in db.execute("SELECT * FROM events ORDER BY id")]

    @staticmethod
    def _check(db, expected_revision):
        state = db.execute("SELECT phase, revision FROM workflow WHERE id = 1").fetchone()
        if state["revision"] != expected_revision:
            raise WorkflowError("Stale revision; read the current state before retrying")
        if state["phase"] == "completed":
            raise WorkflowError("Workflow is completed")
        return state["phase"]

    def _record(self, db, revision, kind, payload):
        db.execute("UPDATE workflow SET revision = ? WHERE id = 1", (revision,))
        self._event(db, revision, kind, payload)

    def transition(self, target, expected_revision):
        with self._connection(write=True) as db:
            phase = self._check(db, expected_revision)
            if db.execute("SELECT 1 FROM decisions WHERE answer IS NULL").fetchone():
                raise WorkflowError("Pending human decisions block transitions")
            if target not in TRANSITIONS[phase]:
                raise WorkflowError(f"Invalid transition: {phase} -> {target}")
            if phase == "planning":
                raise WorkflowError("Planning must pass its quality gate through the planning handler")
            if phase == "architecture":
                raise WorkflowError("Architecture must pass its quality gate through the architecture handler")
            if phase == "discovery":
                raise WorkflowError("Discovery must pass its readiness gate through the discovery handler")
            db.execute("UPDATE workflow SET phase = ? WHERE id = 1", (target,))
            self._record(db, expected_revision + 1, "phase_changed", {"from": phase, "to": target})

    def request_decision(self, question, expected_revision):
        if not isinstance(question, str) or not question.strip():
            raise WorkflowError("Question must be nonempty text")
        with self._connection(write=True) as db:
            self._check(db, expected_revision)
            decision_id = db.execute("INSERT INTO decisions(question) VALUES (?)", (question,)).lastrowid
            self._record(db, expected_revision + 1, "decision_requested",
                         {"id": decision_id, "question": question})
            return decision_id

    def answer_decision(self, decision_id, answer, expected_revision):
        if not isinstance(answer, str) or not answer.strip():
            raise WorkflowError("Answer must be nonempty text")
        with self._connection(write=True) as db:
            self._check(db, expected_revision)
            updated = db.execute("""UPDATE decisions SET answer = ?,
                answered_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE id = ? AND answer IS NULL""", (answer, decision_id)).rowcount
            if not updated:
                raise WorkflowError("Decision does not exist or is already answered")
            if db.execute("SELECT phase FROM workflow WHERE id = 1").fetchone()[0] == "discovery":
                from .discovery import remember_decision
                remember_decision(db, decision_id, answer)
            self._record(db, expected_revision + 1, "decision_answered", {"id": decision_id, "answer": answer})
