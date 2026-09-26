"""Deployment gates: provenance, expiry, task failures, image identity and smoke tests."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import aws_deployment as deploy
import lab

ACCOUNT = '123456789012'
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
STACK = f'arn:aws:cloudformation:eu-west-1:{ACCOUNT}:stack/delivery-lab-dev/unique'
CLUSTER = f'arn:aws:ecs:eu-west-1:{ACCOUNT}:cluster/delivery-lab-dev'


def foundation():
    values = {'ExpiresAt': (datetime.now(timezone.utc) + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S'),
              'EnvironmentName': 'dev', 'ClusterArn': CLUSTER, 'TaskSubnetIds': 'subnet-a,subnet-b',
              'TaskSecurityGroupId': 'sg-dev', 'AllowedClientCidr': '8.8.8.8/32',
              'LoadBalancerDnsName': 'example.eu-west-1.elb.amazonaws.com'}
    return {'StackId': STACK, 'StackStatus': 'CREATE_COMPLETE',
            'Tags': [{'Key': 'Project', 'Value': 'delivery-lab'}, {'Key': 'Purpose', 'Value': 'academic-lab'}],
            'Parameters': [], 'Outputs': [{'OutputKey': k, 'OutputValue': v} for k, v in values.items()]}


class ReleaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        stage = self.home / 'stage'
        files = ['backend.jar', 'frontend/index.html', 'frontend/release.json', 'infra/compose.yaml',
                 'infra/backend.Dockerfile', 'infra/frontend.Dockerfile', 'infra/nginx.conf', 'infra/init-db.sh']
        for name in files:
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'release': SHA}) if name.endswith('release.json') else 'verified')
        lab.write_json(stage / 'manifest.json', {'release': SHA,
                       'checksums': {name: lab.sha256(stage / name) for name in files}})
        self.archive = self.home / 'release.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as bundle:
            for path in stage.rglob('*'):
                if path.is_file():
                    bundle.add(path, arcname=str(path.relative_to(stage)))
        self.data = {'schema_version': 1, 'release': SHA, 'platform': 'linux/amd64',
                     'aws_account': ACCOUNT, 'aws_region': 'eu-west-1',
                     'source': {'repository': 'owner/repo', 'ci_run_id': '123'},
                     'artifact': {'name': f'release-{SHA}', 'file': 'release.tar.gz',
                                  'archive_sha256': lab.sha256(self.archive),
                                  'manifest_sha256': lab.sha256(stage / 'manifest.json')}, 'images': {}}
        for service in ('backend', 'frontend'):
            repo = f'{ACCOUNT}.dkr.ecr.eu-west-1.amazonaws.com/delivery-lab/{service}'
            self.data['images'][service] = {'repository': repo, 'tag': SHA, 'digest': DIGEST, 'uri': repo + '@' + DIGEST}

    def validate(self):
        metadata = self.home / 'aws-release.json'
        lab.write_json(metadata, self.data)
        with tempfile.TemporaryDirectory() as extracted:
            return deploy.validate_release(self.archive, metadata, Path(extracted), ACCOUNT,
                                           'eu-west-1', SHA, 'owner/repo', '123')

    def test_matching_archive_and_metadata_are_accepted(self):
        self.assertEqual(self.validate(), self.data)

    def test_wrong_source_commit_account_region_or_ci_run_is_rejected(self):
        for key in ('release', 'aws_account', 'aws_region', 'platform'):
            original = self.data[key]
            self.data[key] = 'wrong'
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.validate()
            self.data[key] = original
        self.data['source']['ci_run_id'] = '456'
        with self.assertRaisesRegex(ValueError, 'another repository or CI run'):
            self.validate()

    def test_changed_archive_and_wrong_registry_are_rejected(self):
        original = self.data['artifact']['archive_sha256']
        self.data['artifact']['archive_sha256'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'archive does not match'):
            self.validate()
        self.data['artifact']['archive_sha256'] = original
        self.data['images']['backend']['repository'] = 'another-registry/backend'
        with self.assertRaisesRegex(ValueError, 'lab repositories'):
            self.validate()

    def test_ecr_digest_mismatch_is_rejected(self):
        aws = Mock(account=ACCOUNT)
        aws.call.return_value = {'images': [{'imageId': {'imageDigest': 'sha256:' + 'c' * 64}}]}
        with self.assertRaisesRegex(ValueError, 'published release digest'):
            deploy.resolve_images(aws, self.data)

    def test_oci_index_resolves_only_the_linux_amd64_runtime_digest(self):
        aws = Mock(account=ACCOUNT)
        runtime_digest = 'sha256:' + 'c' * 64
        manifest = {'manifests': [{'platform': {'os': 'linux', 'architecture': 'amd64'}, 'digest': runtime_digest},
                                  {'platform': {'os': 'unknown', 'architecture': 'unknown'}, 'digest': DIGEST}]}
        aws.call.return_value = {'images': [{'imageId': {'imageDigest': DIGEST}, 'imageManifest': json.dumps(manifest)}]}
        self.assertEqual(deploy.resolve_images(aws, self.data), {'frontend': runtime_digest, 'backend': runtime_digest})


class DeploymentGateTests(unittest.TestCase):
    def setUp(self):
        self.aws = Mock(account=ACCOUNT)
        self.aws.describe.return_value = foundation()
        self.data = {'release': SHA, 'images': {'backend': {'digest': DIGEST}, 'frontend': {'digest': DIGEST}},
                     'source': {}, 'artifact': {}}
        self.deployment = deploy.Deployment(self.aws, foundation(), Path('/unused'), self.data)

    def test_expired_missing_and_unowned_foundations_are_rejected(self):
        with self.assertRaises(ValueError):
            deploy.check_foundation(None, ACCOUNT)
        stack = foundation()
        stack['Outputs'][0]['OutputValue'] = '2020-01-01T00:00:00'
        with self.assertRaisesRegex(ValueError, 'insufficient time'):
            deploy.check_foundation(stack, ACCOUNT)
        stack = foundation()
        stack['Tags'] = []
        with self.assertRaisesRegex(ValueError, 'ownership'):
            deploy.check_foundation(stack, ACCOUNT)

    def test_runner_ip_must_match_the_restricted_client_cidr(self):
        with patch.object(deploy.urllib.request, 'urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = b'9.9.9.9\n'
            with self.assertRaisesRegex(ValueError, 'Runner public IPv4'):
                deploy.check_runner_network('8.8.8.8/32')

    def test_task_launch_failure_blocks_deployment(self):
        self.aws.call.return_value = {'failures': [{'reason': 'RESOURCE:MEMORY'}], 'tasks': []}
        arn = f'arn:aws:ecs:eu-west-1:{ACCOUNT}:task-definition/delivery-lab-dev-migration-bootstrap:1'
        with self.assertRaisesRegex(ValueError, 'did not start'):
            self.deployment.run_task(arn, 'bootstrap')

    def test_nonzero_or_missing_exit_code_never_counts_as_success(self):
        arn = f'arn:aws:ecs:eu-west-1:{ACCOUNT}:task-definition/delivery-lab-dev-migration-migrate:1'
        for code in (1, None):
            self.aws.call.side_effect = [{'tasks': [{'taskArn': 'task'}]}, {'tasks': [{
                'lastStatus': 'STOPPED', 'stopCode': 'EssentialContainerExited',
                'containers': [{'name': 'migrate', 'exitCode': code}]}]}]
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, 'migrate failed'):
                self.deployment.run_task(arn, 'migrate')

    def test_wait_failure_stops_the_launched_task(self):
        arn = f'arn:aws:ecs:eu-west-1:{ACCOUNT}:task-definition/delivery-lab-dev-migration-migrate:1'
        self.aws.call.side_effect = [{'tasks': [{'taskArn': 'task'}]}, ValueError('API failed'), {}]
        with self.assertRaisesRegex(ValueError, 'API failed'):
            self.deployment.run_task(arn, 'migrate')
        self.assertEqual(self.aws.call.call_args.args[:2], ('ecs', 'stop-task'))

    def test_failed_migration_prevents_application_stack_update(self):
        self.deployment.stack = Mock(return_value={'BootstrapTaskDefinitionArn': 'bootstrap', 'MigrationTaskDefinitionArn': 'migrate'})
        self.deployment.run_task = Mock(side_effect=['bootstrap-task', ValueError('migration failed')])
        with self.assertRaisesRegex(ValueError, 'migration failed'):
            self.deployment.deploy({})
        self.assertEqual(self.deployment.stack.call_count, 1)
        self.assertEqual(self.deployment.stack.call_args.args[0], '-migration')

    def test_old_revision_after_rollback_is_not_accepted_as_success(self):
        self.aws.call.return_value = {'services': [{'deployments': [{'rolloutState': 'COMPLETED'}],
            'runningCount': 1, 'desiredCount': 1, 'pendingCount': 0, 'taskDefinition': 'old'}]}
        with self.assertRaisesRegex(ValueError, 'another revision'):
            self.deployment.verify_service('service', 'new', {})

    def test_running_image_digest_must_match_published_release(self):
        self.aws.call.side_effect = [
            {'services': [{'deployments': [{'rolloutState': 'COMPLETED'}], 'runningCount': 1,
                           'desiredCount': 1, 'pendingCount': 0, 'taskDefinition': 'new'}]},
            {'taskArns': ['task']}, {'tasks': [{'taskDefinitionArn': 'new', 'healthStatus': 'HEALTHY',
                                             'containers': [{'name': 'backend', 'imageDigest': 'wrong'}]}]}]
        with self.assertRaisesRegex(ValueError, 'image digests'):
            self.deployment.verify_service('service', 'new', {'backend': DIGEST})

    def test_smoke_failure_cannot_return_a_passed_receipt(self):
        self.deployment.stack = Mock(side_effect=[
            {'BootstrapTaskDefinitionArn': 'bootstrap', 'MigrationTaskDefinitionArn': 'migrate'},
            {'ServiceArn': 'service', 'ApplicationTaskDefinitionArn': 'definition'}])
        self.deployment.run_task = Mock(return_value='task')
        self.deployment.verify_service = Mock(return_value=['running-task'])
        with patch.object(deploy, 'smoke', side_effect=ValueError('CRUD failed')):
            with self.assertRaisesRegex(ValueError, 'CRUD failed'):
                self.deployment.deploy({})

    def test_foreign_session_child_stack_is_never_updated(self):
        self.aws.describe.side_effect = [foundation(), {'Tags': [{'Key': 'ParentStackId', 'Value': 'another-session'}]}]
        with patch.object(deploy.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'another environment session'):
                self.deployment.stack('-app', 'application.yaml', {})
            run.assert_not_called()

    def test_passed_receipt_is_returned_only_after_all_deployment_gates(self):
        calls = []
        def apply(suffix, *args):
            calls.append(suffix)
            return ({'BootstrapTaskDefinitionArn': 'bootstrap', 'MigrationTaskDefinitionArn': 'migrate'}
                    if suffix == '-migration' else {'ServiceArn': 'service', 'ApplicationTaskDefinitionArn': 'definition'})
        self.deployment.stack = Mock(side_effect=apply)
        self.deployment.run_task = Mock(side_effect=lambda definition, container: calls.append(container) or 'task')
        self.deployment.verify_service = Mock(side_effect=lambda *args: calls.append('verify') or ['task'])
        with patch.object(deploy, 'smoke', side_effect=lambda *args: calls.append('smoke')):
            receipt = self.deployment.deploy({})
        self.assertEqual(calls, ['-migration', 'bootstrap', 'migrate', '-app', 'verify', 'smoke'])
        self.assertEqual(receipt['status'], 'passed')
        self.assertEqual(receipt['images'], self.data['images'])
        self.assertEqual(receipt['foundation_stack_id'], STACK)

    def test_github_deployment_rejects_personal_runner_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = str(Path(temporary) / 'receipt.json')
            argv = ['deploy', '--archive', 'archive', '--metadata', 'metadata', '--output', receipt,
                    '--release', SHA, '--repository', 'owner/repo', '--ci-run-id', '123',
                    '--account', ACCOUNT, '--require-role', 'delivery-lab-github-dev']
            self.aws.verify.return_value = {'Arn': f'arn:aws:iam::{ACCOUNT}:user/personal'}
            with patch.object(sys, 'argv', argv), patch.object(deploy, 'validate_release', return_value={}), \
                    patch.object(deploy, 'Aws', return_value=self.aws):
                with self.assertRaisesRegex(ValueError, 'temporary GitHub DEV role'):
                    deploy.main()
            self.aws.describe.assert_not_called()
            self.assertEqual(lab.read_json(receipt)['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
