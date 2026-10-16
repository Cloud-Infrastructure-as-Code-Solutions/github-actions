"""Exercise the workflows' actual shell programs without Docker or AWS access.

Requires Python, PyYAML, bash, and jq on PATH. On Windows, set BASH to Git Bash.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def step(workflow, name):
    document = yaml.safe_load((ROOT / '.github/workflows' / workflow).read_text())
    return next(s['run'] for j in document['jobs'].values()
                for s in j.get('steps', []) if s.get('name') == name)


class WorkflowTests(unittest.TestCase):
    def run_shell(self, script, env=None, files=None, prelude=''):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in (files or {}).items():
                (root / name).write_text(content)
            (root / 'output').touch()
            process = subprocess.run(
                [os.environ.get('BASH', 'bash'), '-c', prelude + '\n' + script],
                cwd=root,
                env={**os.environ, 'GITHUB_OUTPUT': 'output',
                     'GITHUB_REPOSITORY_OWNER': 'ExampleOrg',
                     'GITHUB_RUN_ID': '123', **(env or {})},
                text=True, capture_output=True, timeout=15,
            )
            return process, (root / 'output').read_text()

    def test_version_sources_and_commit_output(self):
        script = step('build-scan-push.yml', 'Compute image tags')
        for fmt, name, content in [('text', 'VERSION', '1.2.3\r\n'),
                                   ('json', 'package.json', '{"version":"1.2.3"}')]:
            with self.subTest(fmt=fmt):
                result, output = self.run_shell(script, {
                    'IMAGE_NAME': 'app', 'VERSION_FILE': name, 'VERSION_FORMAT': fmt,
                    'GHCR_ENABLED': 'true', 'ECR_ENABLED': 'false',
                }, {name: content}, 'git() { echo abc1234; }')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('version_tag=ghcr.io/exampleorg/app:1.2.3', output)
                self.assertIn('sha_short=abc1234', output)

    def test_no_registry_and_invalid_tags_fail(self):
        for version, ghcr in [('1.2.3', 'false'), ('invalid tag', 'true')]:
            result, _ = self.run_shell(step('build-scan-push.yml', 'Compute image tags'), {
                'IMAGE_NAME': 'app', 'VERSION_FILE': 'VERSION', 'VERSION_FORMAT': 'text',
                'GHCR_ENABLED': ghcr, 'ECR_ENABLED': 'false',
            }, {'VERSION': version})
            self.assertNotEqual(result.returncode, 0)

    def test_independent_registry_outcomes(self):
        script = step('build-scan-push.yml', 'Report publish outcome')
        for ghcr, ecr, expected in [('success', 'failure', 0),
                                    ('failure', 'success', 0),
                                    ('failure', 'failure', 1),
                                    ('skipped', 'skipped', 1)]:
            with self.subTest(ghcr=ghcr, ecr=ecr):
                result, output = self.run_shell(script, {
                    'GHCR': ghcr, 'ECR': ecr,
                    'GHCR_ENABLED': 'true', 'ECR_ENABLED': 'true',
                })
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertIn(f'ghcr={ghcr}', output)
                self.assertIn(f'ecr={ecr}', output)

    def test_ecr_retags_and_pushes_only_current_tags(self):
        result, _ = self.run_shell(step('build-scan-push.yml', 'Push to ECR'), {
            'REGISTRY': 'registry.example', 'REPOSITORY': 'app/service',
            'IMAGE': 'ghcr.io/example/app', 'SHA': 'abc1234', 'VERSION': '1.2.3',
        }, prelude='docker() { printf "%s\\n" "$*"; }')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('push registry.example/app/service:'), 3)
        self.assertNotIn('--all-tags', result.stdout)

    def test_ssm_terminal_states_and_api_errors(self):
        script = step('deploy-ec2.yml', 'Deploy service and wait for completion')
        prelude = '''
          sleep() { :; }
          aws() {
            if [ "$2" = send-command ]; then
              echo command-123
            elif [ "$TEST_STATUS" = AccessDenied ]; then
              echo AccessDeniedException >&2; return 1
            else
              printf '{"Status":"%s","StandardOutputContent":"remote output","StandardErrorContent":""}' "$TEST_STATUS"
            fi
          }
        '''
        for status in ['Success', 'Failed', 'Cancelled', 'TimedOut', 'AccessDenied']:
            with self.subTest(status=status):
                result, output = self.run_shell(script, {
                    'DOC': 'deploy-service', 'INSTANCE': 'i-123', 'SERVICE': 'api',
                    'TAG': 'sha-abc1234', 'WAIT_SECONDS': '1', 'TEST_STATUS': status,
                }, prelude=prelude)
                self.assertEqual(result.returncode, 0 if status == 'Success' else 1,
                                 result.stderr)
                self.assertIn('command-id=command-123', output)

    def test_ssm_eventual_consistency_and_timeout(self):
        prelude = '''
          sleep() { :; }
          aws() {
            if [ "$2" = send-command ]; then echo command-123
            elif [ ! -f seen ]; then
              touch seen; echo InvocationDoesNotExist >&2; return 1
            else
              printf '{"Status":"%s","StandardOutputContent":"","StandardErrorContent":""}' "$TEST_STATUS"
            fi
          }
        '''
        for status, expected in [('Success', 0), ('Pending', 1)]:
            result, _ = self.run_shell(
                step('deploy-ec2.yml', 'Deploy service and wait for completion'), {
                    'DOC': 'deploy-service', 'INSTANCE': 'i-123', 'SERVICE': 'api',
                    'TAG': 'sha-abc1234', 'WAIT_SECONDS': '1', 'TEST_STATUS': status,
                }, prelude=prelude)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_publish_follows_scan_and_has_no_failure_override(self):
        document = yaml.safe_load((ROOT / '.github/workflows/build-scan-push.yml').read_text())
        steps = document['jobs']['publish']['steps']
        scan_index = next(i for i, s in enumerate(steps) if s.get('id') == 'scan')
        for i, entry in enumerate(steps):
            if entry.get('id') in ['ghcr_login', 'ghcr_push', 'ghcr_multi_push',
                                   'aws_auth', 'ecr_login', 'ecr_push', 'ecr_multi_push']:
                self.assertGreater(i, scan_index)
                self.assertNotIn('always()', entry.get('if', ''))

    def test_multi_arch_publication_keeps_independent_registry_attempts(self):
        document = yaml.safe_load((ROOT / '.github/workflows/build-scan-push.yml').read_text())
        steps = {s.get('id'): s for s in document['jobs']['publish']['steps'] if s.get('id')}
        for registry in ['ghcr', 'ecr']:
            local = steps[f'{registry}_push']
            multi = steps[f'{registry}_multi_push']
            self.assertIn("inputs.platforms == ''", local['if'])
            self.assertIn("inputs.platforms != ''", multi['if'])
            self.assertTrue(multi['continue-on-error'])
            self.assertTrue(multi['with']['push'])
            self.assertEqual(multi['with']['platforms'], '${{ inputs.platforms }}')
            self.assertEqual(len(multi['with']['tags'].splitlines()), 3)


if __name__ == '__main__':
    unittest.main()
