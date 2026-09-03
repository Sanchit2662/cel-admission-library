"""Exercise test-CRD ownership and failure cleanup without a Kubernetes cluster."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

MOCK = r'''
import json, os, sys
from pathlib import Path
import yaml
state_file = Path(os.environ['CRD_STATE'])
s = json.loads(state_file.read_text())
a = sys.argv[1:]
s['calls'].append(a)
rc = 0
if a[0] == 'get':
    if os.environ.get('FAIL_GET'): rc = 1
    elif a[2] in s['objects']: print('customresourcedefinition/' + a[2])
elif a[0] == 'create':
    crd = yaml.safe_load(Path(a[a.index('-f') + 1]).read_text())
    name = crd['metadata']['name']
    if os.environ.get('RACE_NAME') == name:
        s['objects'][name] = {'production': True}
    if name in s['objects'] or os.environ.get('FAIL_CREATE') == name: rc = 1
    else: s['objects'][name] = crd['metadata']['labels']
elif a[0] == 'wait':
    if os.environ.get('FAIL_WAIT'): rc = 1
elif a[0] == 'delete':
    selector = next(x.split('=', 1)[1] for x in a if x.startswith('--selector='))
    key, value = selector.split('=', 1)
    s['objects'] = {n: labels for n, labels in s['objects'].items() if labels.get(key) != value}
else: rc = 99
state_file.write_text(json.dumps(s))
sys.exit(rc)
'''


class AgentCRDOwnershipTests(unittest.TestCase):
    def run_setup(self, objects=None, ending='exit 0', **options):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            state = path / 'state.json'
            state.write_text(json.dumps({'objects': objects or {}, 'calls': []}))
            executable = path / 'kubectl'
            executable.write_text('#!' + sys.executable + '\n' + MOCK)
            executable.chmod(0o755)
            env = dict(os.environ, PATH=str(path) + os.pathsep + os.environ['PATH'],
                       CRD_STATE=str(state), PYTHON_EXECUTABLE=sys.executable, **options)
            script = 'source "$1"; setup_agent_test_crds "$2" || exit 1; ' + ending
            result = subprocess.run(['bash', '-c', script, 'test',
                                     str(ROOT / 'scripts/agent-test-crds.sh'),
                                     str(ROOT / 'test-resources/agent-sandbox-test-crds.yaml')],
                                    env=env, capture_output=True, text=True)
            return result, json.loads(state.read_text())

    def test_success_waits_and_removes_only_owned_crds(self):
        result, state = self.run_setup({'unrelated.example.com': {'production': True}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state['objects'], {'unrelated.example.com': {'production': True}})
        self.assertEqual(sum(c[0] == 'create' for c in state['calls']), 3)
        self.assertEqual(sum(c[0] == 'wait' for c in state['calls']), 3)
        self.assertFalse(any(c[0] == 'apply' for c in state['calls']))

    def test_existing_target_is_untouched(self):
        objects = {'workerpools.ate.dev': {'production': True}}
        result, state = self.run_setup(objects)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already exists', result.stderr)
        self.assertEqual(state['objects'], objects)
        self.assertFalse(any(c[0] in ['create', 'delete'] for c in state['calls']))

    def test_preflight_api_error_creates_nothing(self):
        result, state = self.run_setup(FAIL_GET='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] in ['create', 'delete'] for c in state['calls']))

    def test_partial_creation_failure_cleans_up(self):
        result, state = self.run_setup(FAIL_CREATE='workerpools.ate.dev')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state['objects'], {})
        self.assertFalse(any(c[0] == 'wait' for c in state['calls']))

    def test_create_race_does_not_delete_production_crd(self):
        result, state = self.run_setup(RACE_NAME='workerpools.ate.dev')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state['objects'], {'workerpools.ate.dev': {'production': True}})

    def test_wait_failure_cleans_up(self):
        result, state = self.run_setup(FAIL_WAIT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state['objects'], {})

    def test_early_failure_cleans_up(self):
        result, state = self.run_setup(ending='exit 7')
        self.assertEqual(result.returncode, 7)
        self.assertEqual(state['objects'], {})

    def test_termination_cleans_up(self):
        result, state = self.run_setup(ending='kill -TERM $$')
        self.assertEqual(result.returncode, 143)
        self.assertEqual(state['objects'], {})


if __name__ == '__main__':
    unittest.main()
