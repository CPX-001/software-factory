import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from factory.__main__ import converse
from factory.codex_discovery import CodexDiscovery
from factory.discovery import Discovery, MAX_CONTEXT_BYTES, evaluate_readiness, model_context
from factory.discovery_contract import CRITERIA, RESPONSE_SCHEMA
from factory.workflow import Store, WorkflowError, next_action
from tests.discovery_fakes import FakeModel, complete_reply, item, question, reply


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.store = Store(self.project)
        self.store.initialize()

    def test_incremental_upserts_and_pending_questions_survive_sessions(self):
        first = reply(knowledge=[item("vision", "Analizar GitHub")])
        Discovery(self.store, FakeModel(first)).submit("Quiero analizar GitHub")
        second = reply(knowledge=[item("users", "Fundadores"), item("vision", "Encontrar oportunidades en GitHub")],
                       questions=[question("scope")])
        second["resolve_questions"] = [{"key": "audience", "reason": "Respondido por el usuario"}]
        model = FakeModel(second)
        result = Discovery(Store(self.project), model).submit("Para fundadores")
        self.assertEqual(len(result["discovery"]["knowledge"]), 2)
        self.assertEqual(result["discovery"]["questions"][0]["key"], "scope")
        self.assertEqual(model.contexts[0]["knowledge"][0]["text"], "Analizar GitHub")
        self.assertEqual(model.contexts[0]["user_message"], "Para fundadores")
        self.assertEqual(next_action(result)["action"], "discovery")

    def test_ready_automatically_completes_and_architecture_is_available(self):
        result = Discovery(self.store, FakeModel(complete_reply())).submit("Complete product brief")
        self.assertEqual(result["phase"], "architecture")
        self.assertTrue(result["discovery"]["readiness"]["ready"])
        self.assertIsNotNone(result["discovery"]["completed_at"])
        self.assertEqual(next_action(result), {"action": "architecture", "implemented": True, "stage": "not_started", "blockers": []})
        self.assertEqual([e["kind"] for e in self.store.events()],
                         ["initialized", "discovery_input", "discovery_updated", "phase_changed"])
        with self.assertRaises(WorkflowError):
            Discovery(self.store, FakeModel()).submit("Otra cosa")

    def test_model_ready_is_not_enough_and_direct_transition_cannot_bypass_gate(self):
        response = reply(knowledge=[item("vision")], ready=True, questions=[question()])
        result = Discovery(self.store, FakeModel(response)).submit("SaaS")
        self.assertEqual(result["phase"], "discovery")
        self.assertIn("scope", result["discovery"]["readiness"]["missing_criteria"])
        with self.assertRaises(WorkflowError):
            self.store.transition("architecture", result["revision"])
        with self.assertRaises(WorkflowError):
            self.store.transition("requirements", result["revision"])

    def test_pending_decision_blocks_even_when_model_claims_ready_then_natural_answer_advances(self):
        first = complete_reply()
        first["decisions"] = [question("public_only")]
        result = Discovery(self.store, FakeModel(first)).submit("Complete brief; choose public scope")
        self.assertEqual(result["phase"], "discovery")
        self.assertEqual(next_action(result)["action"], "wait_for_human")
        second = complete_reply()
        second["knowledge"] = []  # existing evidence remains sufficient, no full replacement
        second["decision_answers"] = [{"id": 1, "quote": "Solo públicos"}]
        result = Discovery(Store(self.project), FakeModel(second)).submit("Solo públicos")
        self.assertEqual(result["phase"], "architecture")
        self.assertEqual(result["decisions"][0]["answer"], "Solo públicos")
        self.assertTrue(any(k["key"] == "human_decision_1" for k in result["discovery"]["knowledge"]))

    def test_cannot_invent_human_answer_and_invalid_response_is_atomic(self):
        self.store.request_decision("Públicos o privados?", 0)
        response = complete_reply()
        response["decision_answers"] = [{"id": 1, "quote": "Públicos"}]
        with self.assertRaisesRegex(WorkflowError, "exact quote"):
            Discovery(self.store, FakeModel(response)).submit("No lo he decidido")
        result = self.store.snapshot()
        self.assertEqual(result["discovery"]["knowledge"], [])
        self.assertIsNone(result["decisions"][0]["answer"])
        self.assertIsNotNone(result["discovery"]["pending_turn"])
        self.assertEqual(result["revision"], 2)  # decision plus durable input; no model mutation

    def test_every_pending_decision_must_be_answered(self):
        self.store.request_decision("Alcance?", 0)
        self.store.request_decision("Datos?", 1)
        response = complete_reply()
        response["decision_answers"] = [{"id": 1, "quote": "MVP"}]
        result = Discovery(self.store, FakeModel(response)).submit("MVP")
        self.assertEqual(result["discovery"]["readiness"]["pending_decisions"], [2])
        self.assertEqual(result["phase"], "discovery")

    def test_external_human_answer_is_in_snapshot_without_event_history(self):
        self.store.request_decision("Alcance?", 0)
        self.store.answer_decision(1, "Solo público", 1)
        model = FakeModel(reply())
        Discovery(self.store, model).submit("Ese es el alcance")
        context = model.contexts[0]
        self.assertEqual(context["pending_decisions"], [])
        self.assertIn("Solo público", context["knowledge"][0]["text"])

    def test_gate_blocks_material_unknowns_conflicts_and_unconfirmed_core_intent(self):
        for status, category, blocking in (("unknown", "technical_preferences", True),
                                           ("conflict", "technical_preferences", True),
                                           ("assumption", "users", False)):
            with self.subTest(status=status):
                response = complete_reply()
                response["knowledge"] = [k for k in response["knowledge"] if k["key"] != category]
                response["knowledge"].append(item(category, status=status, blocking=blocking))
                gate = evaluate_readiness(response["knowledge"], [], [], response["assessment"])
                self.assertFalse(gate["ready"])
        response = complete_reply()
        response["knowledge"].append(item("technical_preferences", status="unknown"))
        for knowledge in response["knowledge"]:
            if knowledge["category"] in ("scale", "constraints"):
                knowledge["status"] = "assumption"
        self.assertTrue(evaluate_readiness(response["knowledge"], [], [], response["assessment"])["ready"])

    def test_pending_questions_require_explicit_resolution(self):
        Discovery(self.store, FakeModel(reply())).submit("Idea")
        result = Discovery(self.store, FakeModel(complete_reply())).submit("Complete brief")
        self.assertFalse(result["discovery"]["readiness"]["ready"])
        final = complete_reply()
        final["knowledge"] = []
        final["resolve_questions"] = [{"key": "audience", "reason": "Fundadores ya registrados"}]
        self.assertEqual(Discovery(self.store, FakeModel(final)).submit("Fundadores")["phase"], "architecture")

    def test_conflict_is_durable_and_must_be_reconciled_before_completion(self):
        first = complete_reply()
        conflict = item("technical_preferences", "Se exige acceso privado y a la vez prohibir toda autorización.",
                        status="conflict", blocking=True)
        first["knowledge"].append(conflict)
        first["decisions"] = [question("resolve_access")]
        result = Discovery(self.store, FakeModel(first)).submit("Requisitos incompatibles")
        self.assertEqual(result["discovery"]["readiness"]["blocking_items"], ["technical_preferences"])
        second = complete_reply()
        second["knowledge"] = []
        second["decision_answers"] = [{"id": 1, "quote": "Solo públicos"}]
        second["questions"] = [question("reconcile")]
        result = Discovery(Store(self.project), FakeModel(second)).submit("Solo públicos")
        self.assertEqual(result["phase"], "discovery")  # answering alone doesn't erase the conflict
        final = complete_reply()
        final["knowledge"] = [dict(conflict, status="superseded", blocking=False,
                                   basis="La decisión humana elimina los repositorios privados.")]
        final["resolve_questions"] = [{"key": "reconcile", "reason": "Conflicto resuelto con la decisión 1"}]
        model = FakeModel(final)
        result = Discovery(self.store, model).submit("Eliminamos el requisito de repositorios privados")
        self.assertEqual(result["phase"], "architecture")
        self.assertNotIn("technical_preferences", [i["key"] for i in result["discovery"]["knowledge"]])

    def test_oversize_input_is_rejected_before_persistence_or_model_call(self):
        model = FakeModel()
        for message in ("x" * 8001, "🧪" * 4001, "\x00" * 3000, "   "):
            with self.assertRaises(WorkflowError):
                Discovery(self.store, model).submit(message)
        self.assertEqual(model.contexts, [])
        self.assertEqual(len(self.store.events()), 1)

    def test_sdk_failure_retains_input_and_resume_does_not_duplicate_input(self):
        with self.assertRaises(RuntimeError):
            Discovery(self.store, FakeModel(RuntimeError("offline"))).submit("Idea durable")
        with self.assertRaisesRegex(WorkflowError, "pending"):
            Discovery(self.store, FakeModel()).submit("Otra idea")
        model = FakeModel(reply())
        result = Discovery(Store(self.project), model).resume()
        self.assertEqual(model.contexts[0]["user_message"], "Idea durable")
        self.assertIsNone(result["discovery"]["pending_turn"])
        self.assertEqual(len(self.store.events()), 3)

    def test_context_does_not_need_transcript_or_old_assistant_messages(self):
        Discovery(self.store, FakeModel(reply(knowledge=[item("vision", "Persistent fact")]))).submit("UNIQUE_OLD_TRANSCRIPT")
        with sqlite3.connect(self.store.path) as db:
            db.execute("DELETE FROM discovery_turns WHERE status = 'completed'")
        response = reply(questions=[])
        model = FakeModel(response)
        result = Discovery(Store(self.project), model).submit("Nuevo dato")
        encoded = json.dumps(model.contexts[0])
        self.assertNotIn("UNIQUE_OLD_TRANSCRIPT", encoded)
        self.assertNotIn("He incorporado", encoded)
        self.assertIn("Persistent fact", encoded)
        self.assertEqual(result["phase"], "discovery")

    def test_context_is_bounded_and_never_silently_truncated(self):
        state = self.store.snapshot()
        state["discovery"]["knowledge"] = [item("vision", "x" * MAX_CONTEXT_BYTES)]
        with self.assertRaisesRegex(WorkflowError, "context budget"):
            model_context(state, "next")
        state["discovery"]["knowledge"] = [item("vision")]
        before = len(json.dumps(model_context(state, "next")))
        state["decisions"] = [{"id": i, "question": "Historical", "answer": "done"} for i in range(1000)]
        self.assertEqual(len(json.dumps(model_context(state, "next"))), before)

    def test_duplicate_keys_extra_fields_and_missing_evidence_are_rejected(self):
        responses = []
        extra = reply(); extra["phase"] = "architecture"; responses.append(extra)
        duplicate = reply(knowledge=[item("vision"), item("vision")]); responses.append(duplicate)
        evidence = reply(); evidence["assessment"]["evidence"] = [{"criterion": "vision", "keys": ["missing"]}]; responses.append(evidence)
        no_followup = reply(questions=[]); responses.append(no_followup)
        too_many = reply(questions=[question("a"), question("b")], decisions=[question("c"), question("d")]); responses.append(too_many)
        for response in responses:
            with self.subTest(response=response):
                with tempfile.TemporaryDirectory() as project:
                    store = Store(project); store.initialize()
                    with self.assertRaises(WorkflowError):
                        Discovery(store, FakeModel(response)).submit("Idea")
                    self.assertEqual(store.snapshot()["discovery"]["knowledge"], [])

    def test_stale_model_result_cannot_overwrite_concurrent_change(self):
        store = self.store
        class ConcurrentModel:
            def respond(self, context):
                store.request_decision("Concurrent choice?", store.snapshot()["revision"])
                return complete_reply()
        with self.assertRaisesRegex(WorkflowError, "Stale revision"):
            Discovery(store, ConcurrentModel()).submit("Complete brief")
        self.assertEqual(store.snapshot()["discovery"]["knowledge"], [])
        self.assertEqual(store.snapshot()["phase"], "discovery")

    def test_failed_completion_event_rolls_back_knowledge_and_transition(self):
        with sqlite3.connect(self.store.path) as db:
            db.execute("""CREATE TRIGGER fail_completion BEFORE INSERT ON events
                WHEN NEW.kind = 'phase_changed' BEGIN SELECT RAISE(ABORT, 'failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            Discovery(self.store, FakeModel(complete_reply())).submit("Complete brief")
        result = self.store.snapshot()
        self.assertEqual(result["phase"], "discovery")
        self.assertEqual(result["discovery"]["knowledge"], [])
        self.assertIsNone(result["discovery"]["completed_at"])
        self.assertIsNotNone(result["discovery"]["pending_turn"])

    def test_cli_interactive_loop_needs_no_continue_and_resumes_without_model_call(self):
        first = reply()
        final = complete_reply()
        final["resolve_questions"] = [{"key": "audience", "reason": "Answered"}]
        model = FakeModel(first, final)
        with patch("builtins.input", side_effect=["Idea", "Complete brief"]), patch("sys.stdout", new=io.StringIO()) as out:
            converse(self.store, model=model)
        self.assertEqual(len(model.contexts), 2)
        self.assertIn("Ejecuta architecture para continuar", out.getvalue())
        with tempfile.TemporaryDirectory() as project:
            store = Store(project); store.initialize()
            Discovery(store, FakeModel(reply())).submit("Idea")
            no_call = FakeModel()
            with patch("builtins.input", side_effect=EOFError), patch("sys.stdout", new=io.StringIO()) as out:
                converse(Store(project), model=no_call)
            self.assertEqual(no_call.contexts, [])
            self.assertIn("¿Quién lo usará primero?", out.getvalue())

    def test_discovery_persists_across_real_processes(self):
        code = """import sys
from factory.workflow import Store
from factory.discovery import Discovery
from tests.discovery_fakes import FakeModel, reply, item
store = Store(sys.argv[1])
Discovery(store, FakeModel(reply(knowledge=[item('vision', 'GitHub opportunities')]))).submit('Idea')
"""
        first = subprocess.run([sys.executable, "-c", code, str(self.project)], capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        code2 = """import sys
from factory.workflow import Store
from factory.discovery import Discovery
from tests.discovery_fakes import FakeModel, reply, item
store = Store(sys.argv[1])
model = FakeModel(reply(knowledge=[item('users', 'Founders')], questions=[]))
Discovery(store, model).submit('Founders')
assert model.contexts[0]['knowledge'][0]['text'] == 'GitHub opportunities'
assert model.contexts[0]['pending_questions'][0]['key'] == 'audience'
"""
        second = subprocess.run([sys.executable, "-c", code2, str(self.project)], capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        status = subprocess.run([sys.executable, "-m", "factory", "status", str(self.project)], capture_output=True, text=True)
        self.assertEqual(len(json.loads(status.stdout)["discovery"]["knowledge"]), 2)

    def test_v1_migration_preserves_decisions_events_and_legacy_requirements(self):
        with sqlite3.connect(self.store.path) as db:
            for table in ("discovery_meta", "discovery_items", "discovery_questions", "discovery_decisions", "discovery_turns"):
                db.execute(f"DROP TABLE {table}")
            db.execute("PRAGMA user_version = 1")
            db.execute("UPDATE workflow SET phase = 'requirements'")
        before = self.store.events()
        self.assertEqual(self.store.snapshot()["discovery"]["knowledge"], [])
        self.store.initialize()
        self.assertEqual(self.store.events(), before)
        self.store.transition("architecture", 0)
        self.assertEqual(self.store.snapshot()["phase"], "architecture")

    def test_v1_answered_decisions_become_discovery_knowledge_on_migration(self):
        self.store.request_decision("Alcance?", 0)
        self.store.answer_decision(1, "Solo público", 1)
        with sqlite3.connect(self.store.path) as db:
            for table in ("discovery_meta", "discovery_items", "discovery_questions", "discovery_decisions", "discovery_turns"):
                db.execute(f"DROP TABLE {table}")
            db.execute("PRAGMA user_version = 1")
        before = self.store.events()
        self.store.initialize()
        context = model_context(self.store.snapshot(), "Retomemos")
        self.assertIn("Solo público", context["knowledge"][0]["text"])
        self.assertEqual(self.store.events(), before)
        snapshot = self.store.snapshot()
        self.store.initialize()
        self.assertEqual(snapshot, self.store.snapshot())


class AdapterTests(unittest.TestCase):
    def sdk(self, account_type="chatgpt"):
        codex = MagicMock()
        codex.account.return_value.account = SimpleNamespace(root=SimpleNamespace(type=account_type))
        codex.thread_start.return_value.run.return_value.final_response = json.dumps(reply())
        codex.thread_start.return_value.run.return_value.status = "completed"
        factory = MagicMock()
        factory.return_value.__enter__.return_value = codex
        sdk = SimpleNamespace(Codex=factory, CodexConfig=lambda **kw: SimpleNamespace(**kw),
                              Sandbox=SimpleNamespace(read_only="read-only"),
                              ApprovalMode=SimpleNamespace(deny_all="deny_all"))
        return sdk, codex

    def test_official_sdk_receives_schema_and_fresh_ephemeral_threads(self):
        sdk, codex = self.sdk()
        with patch.dict(sys.modules, {"openai_codex": sdk}):
            adapter = CodexDiscovery()
            adapter.respond({"user_message": "Idea"})
            adapter.respond({"user_message": "Respuesta"})
        self.assertEqual(codex.thread_start.call_count, 2)
        self.assertTrue(codex.thread_start.call_args.kwargs["ephemeral"])
        self.assertEqual(codex.thread_start.call_args.kwargs["sandbox"], "read-only")
        self.assertEqual(codex.thread_start.return_value.run.call_args.kwargs["output_schema"], RESPONSE_SCHEMA)
        config = sdk.Codex.call_args.kwargs["config"]
        self.assertIn('forced_login_method="chatgpt"', config.config_overrides)
        self.assertEqual(config.env["OPENAI_API_KEY"], "")
        codex.login_api_key.assert_not_called()

    def test_api_key_accounts_are_rejected_without_model_call(self):
        sdk, codex = self.sdk("apiKey")
        with patch.dict(sys.modules, {"openai_codex": sdk}), self.assertRaisesRegex(WorkflowError, "API-key fallback"):
            CodexDiscovery().respond({})
        codex.thread_start.assert_not_called()

    def test_non_json_output_is_rejected_without_retry(self):
        sdk, codex = self.sdk()
        codex.thread_start.return_value.run.return_value.final_response = "not JSON"
        with patch.dict(sys.modules, {"openai_codex": sdk}), self.assertRaisesRegex(WorkflowError, "Codex discovery failed"):
            CodexDiscovery().respond({})
        self.assertEqual(codex.thread_start.return_value.run.call_count, 1)

    def test_interrupted_sdk_result_is_never_applied(self):
        sdk, codex = self.sdk()
        codex.thread_start.return_value.run.return_value.status = "interrupted"
        with patch.dict(sys.modules, {"openai_codex": sdk}), self.assertRaisesRegex(WorkflowError, "did not complete"):
            CodexDiscovery().respond({})


if __name__ == "__main__":
    unittest.main()
