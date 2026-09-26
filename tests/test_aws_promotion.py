"""Promotion must verify GitHub provenance, release identity and human acceptance."""
import argparse
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import aws_deployment as deploy
import promote_aws_release as promotion
from test_aws_deployment import ACCOUNT, DIGEST, SHA, foundation

REPOSITORY = 'owner/repo'
START = '2026-09-26T09:00:00Z'
FINISH = '2026-09-26T09:10:00Z'


def run(kind='dev'):
    filename, event = promotion.WORKFLOWS[kind]
    return {'id': 123, 'run_attempt': 1, 'workflow_id': 10, 'path': f'.github/workflows/{filename}',
            'event': event, 'head_branch': 'main', 'head_sha': SHA, 'status': 'completed',
            'conclusion': 'success', 'repository': {'full_name': REPOSITORY},
            'head_repository': {'full_name': REPOSITORY}, 'run_started_at': START, 'updated_at': FINISH}


def receipt(env='dev'):
    return {'status': 'passed', 'environment': env, 'release': SHA,
            'source': {'repository': REPOSITORY, 'ci_run_id': '456'},
            'artifact': {'archive_sha256': 'a' * 64},
            'images': {'frontend': {'digest': DIGEST}, 'backend': {'digest': DIGEST}},
            'foundation_stack_id': foundation()['StackId'].replace('-dev/', f'-{env}/'),
            'task_definition_arn': 'definition', 'service_arn': 'service',
            'completed_at': '2026-09-26T09:05:00Z'}


def environment(env):
    return json.loads(json.dumps(foundation()).replace('delivery-lab-dev', f'delivery-lab-{env}').replace('"dev"', f'"{env}"'))


def app_stack(prior):
    values = {'ReleaseId': SHA, 'ServiceArn': 'service', 'ApplicationTaskDefinitionArn': 'definition',
              'FrontendImageDigest': DIGEST, 'BackendImageDigest': DIGEST}
    return {'StackStatus': 'UPDATE_COMPLETE', 'Tags': [{'Key': 'Project', 'Value': 'delivery-lab'},
            {'Key': 'ParentStackId', 'Value': prior['foundation_stack_id']}],
            'Outputs': [{'OutputKey': k, 'OutputValue': v} for k, v in values.items()]}


class GitHubEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.github = promotion.GitHub(REPOSITORY)

    def test_wrong_workflow_event_branch_repository_or_conclusion_cannot_supply_evidence(self):
        for key, value in [('workflow_id', 11), ('path', '.github/workflows/other.yml'),
                           ('event', 'pull_request'), ('head_branch', 'feature'), ('head_sha', 'b' * 40),
                           ('repository', {'full_name': 'other/repo'}), ('head_repository', {'full_name': 'fork/repo'}),
                           ('conclusion', 'failure'), ('status', 'in_progress')]:
            candidate = run()
            candidate[key] = value
            self.github.api = Mock(side_effect=[{'id': 10}, candidate])
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'expected successful main'):
                self.github.run('123', 'dev', SHA)

    def test_workflow_execution_is_bound_to_main_revision_and_attempt(self):
        github = Mock(repository=REPOSITORY)
        github.base = f'repos/{REPOSITORY}/actions'
        context = {'GITHUB_ACTIONS': 'true', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
                   'GITHUB_SHA': SHA}
        with patch.dict(promotion.os.environ, context):
            github.api.return_value = run('uat')
            self.assertEqual(promotion.current_execution(github),
                             promotion.execution_identity(REPOSITORY, run('uat')))
            for key, value in [('head_branch', 'feature'), ('head_sha', 'b' * 40),
                               ('run_attempt', 2), ('event', 'pull_request')]:
                github.api.return_value = dict(run('uat'), **{key: value})
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'main-branch workflow'):
                    promotion.current_execution(github)

    def artifact_response(self, members=None):
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, 'w') as archive:
            for name, content in (members or {'aws-dev-receipt.json': json.dumps(receipt())}).items():
                archive.writestr(name, content)
        data = blob.getvalue()
        metadata = {'id': 55, 'name': f'aws-dev-{SHA}', 'expired': False, 'created_at': FINISH,
                    'digest': 'sha256:' + hashlib.sha256(data).hexdigest()}
        return metadata, data

    def artifact(self):
        return self.github.artifact(run(), f'aws-dev-{SHA}', 'aws-dev-receipt.json',
                                    self.directory / 'receipt.json', fresh=True)

    def test_valid_artifact_is_checked_against_github_digest(self):
        metadata, data = self.artifact_response()
        self.github.api = Mock(side_effect=[{'artifacts': [metadata]}, data])
        reference = self.artifact()
        self.assertEqual(reference['artifact_digest'], metadata['digest'])
        self.assertEqual(reference['run_attempt'], 1)

    def test_absent_duplicate_expired_or_stale_artifacts_fail(self):
        metadata, _ = self.artifact_response()
        variants = [[], [metadata, metadata], [dict(metadata, expired=True)],
                    [dict(metadata, created_at='2026-09-25T09:00:00Z')]]
        for artifacts in variants:
            self.github.api = Mock(return_value={'artifacts': artifacts})
            with self.subTest(artifacts=artifacts), self.assertRaises(ValueError):
                self.artifact()

    def test_tampered_artifact_bytes_are_rejected(self):
        metadata, data = self.artifact_response()
        self.github.api = Mock(side_effect=[{'artifacts': [metadata]}, data + b'changed'])
        with self.assertRaisesRegex(ValueError, 'checksum differs'):
            self.artifact()

    def test_artifact_path_traversal_or_unexpected_files_are_rejected(self):
        metadata, data = self.artifact_response({'../../outside': 'bad'})
        self.github.api = Mock(side_effect=[{'artifacts': [metadata]}, data])
        with self.assertRaisesRegex(ValueError, 'Unexpected contents'):
            self.artifact()
        self.assertFalse((self.directory / 'receipt.json').exists())

    def test_failed_wrong_environment_wrong_release_or_stale_receipt_is_rejected(self):
        self.github.run = Mock(return_value=run())
        for key, value in [('status', 'failed'), ('environment', 'uat'), ('release', 'b' * 40),
                           ('completed_at', '2026-09-25T09:05:00Z')]:
            candidate = dict(receipt(), **{key: value})
            def download(*args, **kwargs):
                args[3].write_text(json.dumps(candidate))
                return {}
            self.github.artifact = Mock(side_effect=download)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'does not prove'):
                self.github.evidence('dev', '123', SHA, self.directory)

    def test_uat_requires_github_execution_and_original_dev_evidence(self):
        dev_reference = {'run_id': '123', 'artifact_id': 55}
        uat = receipt('uat')
        uat['execution'] = promotion.execution_identity(REPOSITORY, run('uat'))
        uat['promotion'] = {'previous': dev_reference}
        self.github.run = Mock(side_effect=[run('uat'), run()])
        def download(workflow, name, filename, destination, **kwargs):
            is_uat = 'uat' in filename
            destination.write_text(json.dumps(uat if is_uat else receipt()))
            return {'run_id': '789'} if is_uat else dev_reference
        self.github.artifact = Mock(side_effect=download)
        actual, _ = self.github.evidence('uat', '789', SHA, self.directory)
        self.assertEqual(actual, uat)
        for change in ('local', 'other_attempt', 'changed_dev'):
            self.github.run.side_effect = [run('uat'), run()]
            broken = copy.deepcopy(uat)
            if change == 'local':
                broken['execution'] = {'kind': 'local'}
            elif change == 'other_attempt':
                broken['execution']['run_attempt'] = 2
            else:
                broken['promotion']['previous']['artifact_id'] = 56
            def download_broken(workflow, name, filename, destination, **kwargs):
                is_uat = 'uat' in filename
                destination.write_text(json.dumps(broken if is_uat else receipt()))
                return {} if is_uat else dev_reference
            self.github.artifact.side_effect = download_broken
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.github.evidence('uat', '789', SHA, self.directory)


class PromotionGateTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(environment='uat', release=SHA, previous_run_id='123', accept_uat=False,
                       repository=REPOSITORY, account=ACCOUNT, region='eu-west-1', profile=None,
                       require_role=True, verify_only=False)

    def test_prod_without_human_acceptance_fails_before_network_or_aws(self):
        self.args.environment = 'prod'
        with patch.object(promotion, 'GitHub') as github, patch.object(promotion, 'Aws') as aws:
            with self.assertRaisesRegex(ValueError, 'human UAT'):
                promotion.promote(self.args)
            github.assert_not_called()
            aws.assert_not_called()

    def test_invalid_input_and_premature_uat_acceptance_fail(self):
        for env, sha, run_id, accept in [('uat', 'main', '123', False), ('uat', SHA, '../12', False),
                                         ('dev', SHA, '123', False), ('uat', SHA, '123', True)]:
            with self.subTest(env=env, sha=sha), self.assertRaises(ValueError):
                promotion.check_request(env, sha, run_id, accept)

    def test_changed_images_or_archive_cannot_be_promoted(self):
        for key in ('images', 'artifact', 'source', 'release'):
            changed = dict(receipt(), **{key: 'different'})
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'immutable image digests'):
                promotion.same_release(receipt(), changed)

    def test_recreated_or_changed_previous_environment_is_rejected(self):
        prior = receipt()
        aws = Mock(account=ACCOUNT)
        recreated = foundation()
        recreated['StackId'] += '-recreated'
        aws.describe.return_value = recreated
        with self.assertRaisesRegex(ValueError, 'recreated'):
            promotion.verify_previous_environment(aws, prior)
        app = app_stack(prior)
        app['Outputs'][0]['OutputValue'] = 'newer-release'
        aws.describe.side_effect = [foundation(), app]
        with self.assertRaisesRegex(ValueError, 'no longer contains'):
            promotion.verify_previous_environment(aws, prior)

    def test_correct_previous_environment_is_accepted(self):
        aws = Mock(account=ACCOUNT)
        aws.describe.side_effect = [foundation(), app_stack(receipt())]
        promotion.verify_previous_environment(aws, receipt())

    def test_uat_and_prod_engine_use_target_environment_and_same_digests(self):
        for env in ('uat', 'prod'):
            aws = Mock(account=ACCOUNT)
            data = receipt(env)
            deployment = deploy.Deployment(aws, environment(env), Path('/unused'), data, env)
            deployment.stack = Mock(side_effect=[{'BootstrapTaskDefinitionArn': 'bootstrap', 'MigrationTaskDefinitionArn': 'migrate'},
                                                  {'ServiceArn': 'service', 'ApplicationTaskDefinitionArn': 'definition'}])
            deployment.run_task = Mock(return_value='task')
            deployment.verify_service = Mock(return_value=['task'])
            deployment.still_active = Mock()
            with patch.object(deploy, 'smoke') as smoke:
                result = deployment.deploy({'frontend': DIGEST, 'backend': DIGEST})
                self.assertEqual(smoke.call_args.args[-1], env)
            self.assertEqual(result['images'], data['images'])
            self.assertEqual(result['environment'], env)
            self.assertEqual(deployment.stack.call_args.args[2]['EnvironmentStackName'], f'delivery-lab-{env}')

    def test_wrong_target_foundation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'identity'):
            deploy.check_foundation(foundation(), ACCOUNT, environment='uat')

    def test_provenance_failure_prevents_any_aws_operation(self):
        with patch.object(promotion, 'GitHub') as github, patch.object(promotion, 'Aws') as aws, \
                patch.object(promotion, 'current_execution', return_value={'kind': 'local'}):
            github.return_value.evidence.side_effect = ValueError('Invalid source workflow')
            with self.assertRaisesRegex(ValueError, 'Invalid source'):
                promotion.promote(self.args)
            aws.assert_not_called()

    def test_failed_attempt_overwrites_an_older_success_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receipt.json'
            path.write_text(json.dumps(receipt('uat')))
            arguments = ['promote', '--environment', 'uat', '--release', SHA,
                         '--previous-run-id', '123', '--repository', REPOSITORY,
                         '--account', ACCOUNT, '--output', str(path)]
            with patch.object(sys, 'argv', arguments), \
                    patch.object(promotion, 'promote', side_effect=ValueError('smoke failed')):
                with self.assertRaisesRegex(ValueError, 'smoke failed'):
                    promotion.main()
            self.assertEqual(json.loads(path.read_text())['status'], 'failed')

    def test_success_verify_only_wrong_role_and_deployment_failure(self):
        for mode in ('success', 'prod_success', 'verify_only', 'wrong_role', 'deploy_failure'):
            env = 'prod' if mode == 'prod_success' else 'uat'
            self.args.environment = env
            self.args.accept_uat = env == 'prod'
            self.args.verify_only = mode == 'verify_only'
            aws = Mock(account=ACCOUNT)
            aws.verify.return_value = {'Arn': f'arn:aws:sts::{ACCOUNT}:assumed-role/delivery-lab-github-{env}/session'}
            if mode == 'wrong_role':
                aws.verify.return_value = {'Arn': f'arn:aws:iam::{ACCOUNT}:user/personal'}
            aws.describe.return_value = environment(env)
            github = Mock()
            github.evidence.return_value = (receipt('uat' if env == 'prod' else 'dev'), {'run_id': '123'})
            engine = Mock()
            engine.deploy.return_value = receipt(env)
            if mode == 'deploy_failure':
                engine.deploy.side_effect = ValueError('smoke failed')
            with self.subTest(mode=mode), patch.object(promotion, 'GitHub', return_value=github), \
                    patch.object(promotion, 'current_execution', return_value={'kind': 'local'}), \
                    patch.object(promotion, 'validate_release', return_value=receipt()), \
                    patch.object(promotion, 'Aws', return_value=aws), \
                    patch.object(promotion, 'verify_previous_environment'), \
                    patch.object(promotion, 'resolve_images', return_value={}), \
                    patch.object(promotion, 'Deployment', return_value=engine) as constructor:
                if mode in ('wrong_role', 'deploy_failure'):
                    with self.assertRaises(ValueError):
                        promotion.promote(self.args)
                else:
                    result = promotion.promote(self.args)
                    self.assertEqual(result['status'], 'verified-only' if mode == 'verify_only' else 'passed')
                if mode in ('wrong_role', 'verify_only'):
                    constructor.assert_not_called()
                if mode in ('success', 'prod_success'):
                    self.assertEqual(result['promotion']['previous']['run_id'], '123')
                    self.assertEqual(result['promotion']['accepted_uat'], env == 'prod')
                    self.assertEqual(constructor.call_args.args[-1], env)


if __name__ == '__main__':
    unittest.main()
