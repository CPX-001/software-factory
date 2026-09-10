import json
from unittest.mock import patch
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from factory.workflow import Store, WorkflowError, next_action
from factory.discovery import Discovery
from tests.discovery_fakes import FakeModel, complete_reply
from tests.architecture_fakes import finish
from tests.planning_fakes import finish as finish_planning


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        environment = patch.dict('os.environ', {'FACTORY_HOME': str(self.project / 'registry')})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = Store(self.project)
        self.store.initialize()

    def test_initialization_is_idempotent_and_reads_do_not_write(self):
        before = self.store.snapshot()
        self.store.initialize()
        self.assertEqual(before, Store(self.project).snapshot())
        self.assertEqual(next_action(before)["action"], "discovery")
        self.assertEqual(len(self.store.events()), 1)

    def test_valid_progress_and_terminal_state(self):
        Discovery(self.store, FakeModel(complete_reply())).submit("Complete product brief")
        finish(self.store)
        finish_planning(self.store)
        for phase in ("verification", "completed"):
            self.store.transition(phase, self.store.snapshot()["revision"])
        self.assertEqual(next_action(self.store.snapshot())["action"], "done")
        with self.assertRaises(WorkflowError):
            self.store.request_decision("Continue?", self.store.snapshot()["revision"])

    def test_invalid_and_stale_transitions_leave_no_events(self):
        with self.assertRaises(WorkflowError):
            self.store.transition("execution", 0)
        self.store.request_decision("Scope?", 0)
        with self.assertRaises(WorkflowError):
            Store(self.project).transition("architecture", 0)
        self.assertEqual(len(self.store.events()), 2)

    def test_decisions_survive_reopen_and_block_until_all_answered(self):
        first = self.store.request_decision("Which scope?", 0)
        second = self.store.request_decision("Which budget?", 1)
        reopened = Store(self.project)
        self.assertEqual(next_action(reopened.snapshot())["action"], "wait_for_human")
        with self.assertRaises(WorkflowError):
            reopened.transition("architecture", 2)
        reopened.answer_decision(first, "Small", 2)
        self.assertEqual(reopened.snapshot()["status"], "waiting_for_human")
        reopened.answer_decision(second, "100", 3)
        self.assertEqual(next_action(reopened.snapshot())["action"], "discovery")
        with self.assertRaises(WorkflowError):
            reopened.answer_decision(first, "Large", 4)
        self.assertEqual(reopened.snapshot()["decisions"][0]["answer"], "Small")

    def test_verification_can_return_to_execution(self):
        Discovery(self.store, FakeModel(complete_reply())).submit("Complete product brief")
        finish(self.store)
        finish_planning(self.store)
        for phase in ("verification", "execution"):
            self.store.transition(phase, self.store.snapshot()["revision"])
        self.assertEqual(self.store.snapshot()["phase"], "execution")

    def test_event_failure_rolls_back_state(self):
        with sqlite3.connect(self.store.path) as db:
            db.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.request_decision("Scope?", 0)
        self.assertEqual(self.store.snapshot()["revision"], 0)
        self.assertEqual(self.store.snapshot()["phase"], "discovery")

    def test_missing_project_state_is_not_created_by_read(self):
        other = self.project / "other"
        other.mkdir()
        with self.assertRaises(WorkflowError):
            Store(other).snapshot()
        self.assertFalse((other / ".factory").exists())

    def test_cli_across_processes(self):
        def cli(command):
            return subprocess.run([sys.executable, "-m", "factory", command, str(self.project)], capture_output=True, text=True)
        self.assertEqual(cli("init").returncode, 0)
        self.assertEqual(json.loads(cli("status").stdout)["phase"], "discovery")
        self.assertEqual(json.loads(cli("next").stdout)["action"], "discovery")


if __name__ == "__main__":
    unittest.main()
