"""No quota: simulated SDK, real temporary Git repositories and isolated Python checks."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import time

from factory.application import FactoryService
from factory.discovery import Discovery
from factory.execution_contract import DEFAULT_POLICY
from factory.planning import Planning
from factory.registry import Registry
from factory.skill_catalog import Catalog
from factory.skill_router import SkillRouter
from factory.workflow import Store
from tests.discovery_fakes import FakeModel, complete_reply
from tests.architecture_fakes import finish as architecture_finish, FakeArchitect, review
from tests.planning_fakes import proposal
from factory.planning import source_snapshot


def result(value=3, *, tests=False):
    changes = [{'path': 'app.py', 'operation': 'write', 'content': f'def add(a, b):\n    return {value}\n'}]
    if tests:
        changes.append({'path': 'test_app.py', 'operation': 'write', 'content':
            'import unittest\nfrom app import add\nclass Behavior(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n'})
    return dict(summary='Implemented add', changes=changes, criteria_addressed=[0], checks_declared=['tests: PASS'],
                risks=[], questions=[], architecture={'impact': 'none', 'reason': '', 'references': []},
                harness_paths=['test_app.py'] if tests else [], read_paths=[])


class FakeSDK:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.contexts, self.threads = [], []
        self.quota_value = None
        self.init_error = None
        self.on_call = None
        self.usage = {'total': {'totalTokens': 100}}
        self.recovered_result = None

    def __call__(self, sandbox, policy, worktree, skill_inputs):
        if self.init_error:
            raise self.init_error
        self.worktree = Path(worktree)
        self.skills = skill_inputs
        return self

    def quota(self):
        return deepcopy(self.quota_value) if self.quota_value is not None else {
            'observed_at': time.time(), 'buckets': {'codex': {'primary': {'usedPercent': 5, 'resetsAt': time.time() + 10000}}}}

    def respond(self, context, *, thread_id, should_stop, on_runtime, on_usage, on_quota):
        self.contexts.append(context)
        self.threads.append(thread_id)
        on_runtime({'thread_id': thread_id or 'fake-slice-thread', 'turn_id': 'turn-' + str(len(self.contexts)), 'process': None})
        if self.on_call:
            self.on_call(should_stop)
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = value(context)
        on_usage(self.usage)
        return {'response': deepcopy(value), 'worker_status': 'completed', 'usage': self.usage, 'thread_id': 'fake-slice-thread'}

    def recover(self, reference):
        return self.recovered_result

    def close(self):
        pass


def fixture(root, sdk, *, harness_during=False, customize=None):
    project = root / 'product'
    project.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(project), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    git('config', 'user.email', 'test@localhost')
    git('config', 'user.name', 'Test')
    (project / 'app.py').write_text('def add(a, b):\n    return 0\n')
    if not harness_during:
        (project / 'test_app.py').write_text(result(tests=True)['changes'][1]['content'])
    git('add', '.')
    git('commit', '-qm', 'Product baseline')
    store = Store(project); store.initialize()
    Discovery(store, FakeModel(complete_reply())).submit('Small add product')
    architecture_finish(store)
    plan = proposal(source_snapshot(store.snapshot()))
    if not harness_during:
        plan['harness'][0]['when'] = 'before_slice'
    if customize:
        customize(plan)
    router = SkillRouter(Catalog())
    Planning(store, FakeArchitect(plan, review()), router).run()
    registry = Registry(root / 'registry'); registry.allow_root(root)
    registered = registry.register(str(project))
    registry.select(registered['id'])
    jobs = []
    service = FactoryService(registry, router=router, execution_worker_factory=sdk,
                             launcher=lambda p, r: jobs.append((p, r)))
    policy = {**DEFAULT_POLICY, 'enabled': True, 'model': 'test-model', 'effort': 'low',
              'write_paths': ['app.py', 'test_app.py'], 'context_paths': ['app.py', 'test_app.py']}
    verification = {'schema_version': 1, 'checks': [
        {'id': 'behavior', 'gate': 'local_gate', 'kind': 'python_behavior', 'target': 'app:add',
         'cases': [{'args_json': '[1,2]', 'expected_json': '3'}],
         'min_tests': 1, 'timeout_seconds': 5, 'criteria': [0], 'gate_checks': [0]},
        {'id': 'tests', 'gate': 'local_gate', 'kind': 'python_unittest', 'target': 'test_app.py',
         'cases': [], 'min_tests': 1, 'timeout_seconds': 5, 'criteria': [0], 'gate_checks': [0]}],
        'harness': [{'id': 'unit', 'paths': ['test_app.py']}]}
    service.configure_execution(policy, verification)
    return service, store, jobs, git
