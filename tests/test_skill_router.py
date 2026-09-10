from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from factory.__main__ import main
from factory.skill_catalog import Catalog, Skill, catalog_from_response, discover_catalog, _runtime_query
from factory.skill_router import Policy, SkillRouter, Work, load_config
from factory.workflow import WorkflowError


def skill(name, *, enabled=True, path=None):
    return Skill(name, name.rsplit(":", 1)[-1], path or f"/skills/{name}/SKILL.md", enabled=enabled)


def catalog(*names):
    return Catalog(tuple(skill(name) for name in names))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)

    def response(self, entries):
        return {"data": [{"cwd": str(self.project), "errors": [], "skills": entries}]}

    def entry(self, name="debugging-and-error-recovery", path=None, enabled=True):
        return {"name": name, "path": str(path or self.project / name / "SKILL.md"),
                "enabled": enabled, "scope": "user", "description": "Fixture metadata"}

    def test_native_catalog_and_explicit_roots_keep_distinct_provenance(self):
        root = self.project / "extra"
        root.mkdir()
        data = self.response([self.entry(), self.entry("archify", root / "archify" / "SKILL.md")])
        query = MagicMock(return_value=data)
        result = discover_catalog(self.project, [str(root), str(root)], query=query)
        query.assert_called_once_with(str(self.project), [str(root)])
        self.assertEqual(result.resolve("archify")[0].source, "explicit_root")
        self.assertEqual(result.resolve("debugging-and-error-recovery")[0].source, "runtime")

    def test_plugin_manifest_supplies_real_namespace_not_directory_guess(self):
        plugin = self.project / "cache" / "arbitrary-package" / "1.2"
        metadata = plugin / ".codex-plugin"
        metadata.mkdir(parents=True)
        (metadata / "plugin.json").write_text(json.dumps({"name": "agent-skills"}))
        path = plugin / "skills" / "debugging-and-error-recovery" / "SKILL.md"
        result = catalog_from_response(self.response([self.entry(path=path)]), self.project)
        self.assertEqual(result.skills[0].id, "agent-skills:debugging-and-error-recovery")
        self.assertEqual(result.resolve("debugging-and-error-recovery")[0], result.skills[0])

    def test_duplicates_collapse_disabled_stays_disabled_and_other_cwd_is_ignored(self):
        entry = self.entry()
        data = self.response([entry, entry, dict(entry, enabled=False)])
        data["data"].append({"cwd": str(self.project / "other"), "skills": [self.entry("archify")]})
        result = catalog_from_response(data, self.project)
        self.assertEqual(len(result.skills), 1)
        self.assertEqual(result.resolve(entry["name"]), (None, "disabled"))
        self.assertEqual(result.resolve("archify"), (None, "not_discovered"))

    def test_ambiguous_names_require_qualified_selector(self):
        result = catalog("one:audit", "product-design:audit")
        self.assertEqual(result.resolve("audit"), (None, "ambiguous"))
        self.assertEqual(result.resolve("product-design:audit")[0].id, "product-design:audit")
        variants = Catalog((skill("archify", path="/a/SKILL.md"), skill("archify", path="/b/SKILL.md")))
        self.assertEqual(variants.resolve("archify"), (None, "ambiguous"))

    def test_invalid_metadata_and_discovery_errors_are_observable(self):
        response = self.response([self.entry(enabled="true"), self.entry(path="relative/SKILL.md")])
        response["data"][0]["errors"] = [{"message": "broken frontmatter"}]
        result = catalog_from_response(response, self.project)
        self.assertEqual(result.skills, ())
        self.assertEqual(len(result.errors), 3)
        result = discover_catalog(self.project, [str(self.project / "missing")],
                                  query=MagicMock(side_effect=WorkflowError("runtime failed")))
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(result.skills, ())

    def test_empty_or_wrong_project_response_is_not_silent_success(self):
        self.assertTrue(catalog_from_response({"data": []}, self.project).errors)
        with self.assertRaises(WorkflowError):
            catalog_from_response({"data": "bad"}, self.project)

    def test_worker_timeout_degrades_to_catalog_error_without_fabricating_skills(self):
        with patch("factory.skill_catalog.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 45)):
            result = discover_catalog(self.project)
        self.assertEqual(result.skills, ())
        self.assertIn("Catalog discovery failed", result.errors[0])

    def test_sdk_adapter_only_uses_metadata_rpcs_and_closes(self):
        client = MagicMock()
        client.request.return_value.model_dump.return_value = self.response([])
        config_type = lambda **kw: kw
        fake_client = SimpleNamespace(CodexClient=MagicMock(return_value=client), CodexConfig=config_type)
        fake_models = SimpleNamespace(SkillsExtraRootsSetResponse=object, SkillsListResponse=dict)
        with patch.dict(sys.modules, {"openai_codex.client": fake_client, "openai_codex.generated.v2_all": fake_models}):
            _runtime_query(str(self.project), ["/explicit"])
        methods = [call.args[0] for call in client.request.call_args_list]
        self.assertEqual(methods, ["skills/extraRoots/set", "skills/list"])
        self.assertTrue(client.request.call_args.args[1]["forceReload"])
        client.close.assert_called_once()
        client.thread_start.assert_not_called()
        client.turn_start.assert_not_called()
        client.account_read.assert_not_called()

    def test_no_implicit_scan_of_plugin_cache(self):
        plugin = self.project / ".codex" / "plugins" / "cache" / "fake"
        plugin.mkdir(parents=True)
        (plugin / "SKILL.md").write_text("---\nname: archify\ndescription: Cached\n---")
        result = discover_catalog(self.project, query=lambda *_: self.response([]))
        self.assertEqual(result.skills, ())


class RouterTests(unittest.TestCase):
    def test_domain_and_intent_select_real_engineering_skills(self):
        cases = [
            (Work(domain="architecture"), "api-and-interface-design"),
            (Work(domain="api", intent="design"), "api-and-interface-design"),
            (Work(intent="debug"), "debugging-and-error-recovery"),
            (Work(intent="test"), "test-driven-development"),
            (Work(intent="tdd"), "test-driven-development"),
            (Work(intent="refactor"), "code-simplification"),
            (Work(concerns=("performance",)), "performance-optimization"),
            (Work(intent="review"), "code-review-and-quality"),
            (Work(intent="plan"), "planning-and-task-breakdown"),
            (Work(intent="specify"), "spec-driven-development"),
        ]
        available = catalog(*(f"agent-skills:{name}" for _, name in cases))
        # Catalog normally deduplicates identical runtime records.
        available = replace(available, skills=tuple({s.id: s for s in available.skills}.values()))
        for work, name in cases:
            with self.subTest(work=work):
                result = SkillRouter(available).route(work)
                self.assertEqual(result.recommended, [f"agent-skills:{name}"])
                self.assertEqual(result.required, [])

    def test_architecture_diagram_uses_archify_without_general_design_bundle(self):
        result = SkillRouter(catalog("archify", "agent-skills:api-and-interface-design")).route(
            Work(domain="architecture", intent="diagram"))
        self.assertEqual(result.recommended, ["archify"])

    def test_ui_creation_has_one_primary_plus_verified_companion_and_system(self):
        available = catalog("frontend-design-premium:frontend-design-premium", "frontend-design-premium:frontend-design",
                            "ui-ux-pro-max", "design", "ui-styling", "design-system", "visual-polish")
        result = SkillRouter(available).route(Work(domain="frontend", intent="design", concerns=("design-system",)))
        self.assertEqual(result.recommended, ["frontend-design-premium:frontend-design-premium",
                                             "frontend-design-premium:frontend-design", "design-system"])
        self.assertEqual(result.required, [])

    def test_missing_companion_skips_premium_and_tries_an_alternative(self):
        result = SkillRouter(catalog("frontend-design-premium", "ui-ux-pro-max")).route(Work(ui=True, intent="design"))
        self.assertEqual(result.recommended, ["ui-ux-pro-max"])
        self.assertEqual(result.blocked, [])
        self.assertTrue(any(m["reason"].startswith("prerequisite") for m in result.unavailable))
        result = SkillRouter(catalog("frontend-design-premium"), Policy(required=("frontend-design-premium",))).route(Work())
        self.assertTrue(result.blocked)

    def test_review_accessibility_and_product_references_are_intent_specific(self):
        available = catalog("frontend-design", "reviewing-design-work", "frontend-a11y", "wcag-contrast",
                            "product-design:audit", "product-design:ideate", "product-design:image-to-code", "product-design:url-to-code")
        for intent, expected in (("review", "reviewing-design-work"), ("audit", "product-design:audit"),
                                 ("explore", "product-design:ideate"), ("from-image", "product-design:image-to-code"),
                                 ("from-url", "product-design:url-to-code")):
            result = SkillRouter(available).route(Work(domain="frontend", intent=intent))
            self.assertEqual(result.recommended, [expected])
        result = SkillRouter(available).route(Work(ui=True, intent="review", concerns=("accessibility", "contrast-wcag")))
        self.assertEqual(set(result.recommended), {"reviewing-design-work", "frontend-a11y", "wcag-contrast"})

    def test_duplicates_and_equivalent_overrides_are_not_stacked(self):
        available = catalog("frontend-design", "ui-ux-pro-max")
        result = SkillRouter(available, Policy(recommended=("frontend-design", "ui-ux-pro-max", "frontend-design"))).route(
            Work(ui=True, intent="design"))
        self.assertEqual(result.recommended, ["frontend-design"])
        result = SkillRouter(catalog("agent-skills:security-and-hardening"),
                             Policy(required=("security-and-hardening",), recommended=("agent-skills:security-and-hardening",))).route(
                                 Work(concerns=("security",), risk="critical"))
        self.assertEqual(result.required, ["agent-skills:security-and-hardening"])
        self.assertEqual(result.recommended, [])

    def test_budget_counts_companions_and_never_drops_required(self):
        available = catalog("frontend-design-premium", "frontend-design", "ui-ux-pro-max", "frontend-a11y",
                            "agent-skills:security-and-hardening", "agent-skills:performance-optimization")
        result = SkillRouter(available, Policy(max_recommended=1)).route(
            Work(ui=True, intent="design", risk="critical", concerns=("security", "performance", "accessibility")))
        self.assertEqual(result.required, ["agent-skills:security-and-hardening"])
        self.assertEqual(len(result.recommended), 1)
        result = SkillRouter(available, Policy(max_recommended=1)).route(Work(ui=True, intent="design"))
        self.assertEqual(result.recommended, ["ui-ux-pro-max"])
        result = SkillRouter(available, Policy(max_recommended=0)).route(Work(risk="critical", concerns=("security",)))
        self.assertEqual(result.required, ["agent-skills:security-and-hardening"])

    def test_required_missing_blocks_and_recommended_missing_does_not(self):
        missing = SkillRouter(Catalog())
        critical = missing.route(Work(risk="critical", concerns=("security",)))
        self.assertTrue(critical.blocked)
        self.assertEqual(critical.required, [])
        self.assertIsNone(critical.as_dict()["context"])
        with self.assertRaises(WorkflowError):
            critical.context()
        ordinary = missing.route(Work(concerns=("security",)))
        self.assertFalse(ordinary.blocked)
        self.assertTrue(ordinary.unavailable)
        self.assertEqual(ordinary.recommended, [])
        self.assertIn("other available skills", ordinary.context())

    def test_disabled_and_excluded_required_are_not_treated_as_available(self):
        disabled = Catalog((skill("security-and-hardening", enabled=False),))
        self.assertTrue(SkillRouter(disabled).route(Work(risk="critical", concerns=("security",))).blocked)
        excluded = SkillRouter(catalog("security-and-hardening"), Policy(exclude=("security-and-hardening",)))
        result = excluded.route(Work(risk="critical", concerns=("security",)))
        self.assertTrue(result.blocked)
        self.assertIn("excluded_by_project", [m["reason"] for m in result.unavailable])
        prefixed = Catalog((replace(skill("agent-skills:security-and-hardening"),
                                    name="agent-skills:security-and-hardening"),))
        result = SkillRouter(prefixed, Policy(exclude=("security-and-hardening",))).route(
            Work(risk="critical", concerns=("security",)))
        self.assertTrue(result.blocked)

    def test_project_bindings_disable_rules_and_explicit_requirements(self):
        policy = Policy.from_dict({"bindings": {"debugging": ["team:debug"]}, "disabled_rules": ["testing"],
                                   "required": ["team:quality"], "recommended": ["optional:missing"]})
        result = SkillRouter(catalog("team:debug", "team:quality", "test-driven-development"), policy).route(
            Work(intent="debug", concerns=("testing",)))
        self.assertEqual(result.required, ["team:quality"])
        self.assertEqual(result.recommended, ["team:debug"])
        self.assertFalse(result.blocked)
        self.assertEqual(result.unavailable[0]["skill"], "optional:missing")

    def test_compact_context_does_not_include_catalog_descriptions_paths_or_missing(self):
        available = catalog("archify", *[f"unrelated-{i}" for i in range(200)])
        result = SkillRouter(available).route(Work(domain="architecture", intent="diagram"))
        context = result.context()
        self.assertLess(len(context), 500)
        self.assertNotIn("unrelated", context)
        self.assertNotIn("/skills/", context)
        self.assertIn("Recommended skills: archify", context)
        self.assertIn("does not mean a skill has been used", context)

    def test_required_inputs_carry_real_paths_and_do_not_activate_recommendations(self):
        available = catalog("agent-skills:security-and-hardening", "agent-skills:test-driven-development")
        result = SkillRouter(available).route(Work(risk="critical", concerns=("security", "testing")))
        inputs = result.required_inputs(available)
        self.assertEqual(inputs, [{"type": "skill", "name": "security-and-hardening",
                                  "path": "/skills/agent-skills:security-and-hardening/SKILL.md"}])
        with self.assertRaises(WorkflowError):
            result.required_inputs(Catalog())
        blocked = SkillRouter(Catalog()).route(Work(risk="critical", concerns=("security",)))
        with self.assertRaises(WorkflowError):
            blocked.required_inputs(available)

    def test_general_work_never_gets_incidental_engineering_or_coordination_skills(self):
        available = catalog("frontend-design", "test-driven-development", "git-workflow-and-versioning",
                            "security-and-hardening", "orchestrating-agent-delegation",
                            "complex-enough:orchestrate-multi-perspective-panel")
        result = SkillRouter(available).route(Work())
        self.assertEqual(result.recommended, [])
        self.assertEqual(result.required, [])
        self.assertEqual(result.unavailable, [])
        self.assertEqual(SkillRouter(available).route(Work(risk="critical")).recommended, [])

    def test_order_is_deterministic_and_invalid_policy_does_not_silently_weaken_gate(self):
        a = Work(intent="debug", concerns=("testing", "security"))
        b = Work(intent="debug", concerns=("security", "testing", "testing"))
        self.assertEqual(SkillRouter(Catalog()).route(a).as_dict(), SkillRouter(Catalog()).route(b).as_dict())
        for config in ({"max_recommended": True}, {"max_recommended": 50}, {"bindings": {"typo": []}},
                       {"required": "security"}, {"unknown": True}, {"bindings": {"testing": []}}):
            with self.assertRaises(WorkflowError):
                Policy.from_dict(config)
        with self.assertRaises(WorkflowError):
            Work(risk="critcal")
        with self.assertRaises(WorkflowError):
            Work(risk="critical", concerns="security")


class ConfigAndCliTests(unittest.TestCase):
    def test_configuration_is_explicit_separate_and_has_no_database_dependency(self):
        with tempfile.TemporaryDirectory() as project:
            root = Path(project)
            self.assertEqual(load_config(root), ((), Policy()))
            (root / ".factory").mkdir()
            (root / ".factory" / "skills.json").write_text(json.dumps({"roots": ["../skills"],
                                                                       "policy": {"required": ["team:security"]}}))
            roots, policy = load_config(root)
            self.assertEqual(roots, ("../skills",))
            self.assertEqual(policy.required, ("team:security",))
            self.assertFalse((root / ".factory" / "state.sqlite3").exists())

    def test_cli_catalog_routing_and_blocking_exit_code(self):
        with tempfile.TemporaryDirectory() as project:
            for arguments, expected_exit in ((["skills", project], None),
                                             (["route-skills", project, "--domain", "architecture", "--intent", "diagram"], None),
                                             (["route-skills", project, "--risk", "critical", "--concern", "security"], 2)):
                with patch.object(sys, "argv", ["factory", *arguments]), patch("sys.stdout", new=io.StringIO()) as output, \
                     patch("factory.skill_catalog.discover_catalog", return_value=catalog("archify")):
                    if expected_exit:
                        with self.assertRaises(SystemExit) as error:
                            main()
                        self.assertEqual(error.exception.code, expected_exit)
                    else:
                        main()
                    data = json.loads(output.getvalue())
                    if arguments[0] == "skills":
                        self.assertEqual(data["skills"][0]["id"], "archify")
                    elif not expected_exit:
                        self.assertEqual(data["routing"]["recommended"], ["archify"])
                    else:
                        self.assertEqual(data["routing"]["status"], "blocked")
            self.assertFalse((Path(project) / ".factory").exists())


if __name__ == "__main__":
    unittest.main()
