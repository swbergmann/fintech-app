"""Recovery restores only verified images and stops on uncertain database state."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import aws_diagnostics
import recover_aws_release as recovery
from test_aws_deployment import ACCOUNT, DIGEST, SHA, foundation
from test_aws_promotion import receipt, run, REPOSITORY
import promote_aws_release as promotion

NEW_SHA = 'b' * 40
MIGRATION = 'BOOT-INF/classes/db/migration/V1__create_customers.sql'


def jar(directory, changes=None):
    directory.mkdir(parents=True, exist_ok=True)
    files = {MIGRATION: 'CREATE TABLE customers(id NUMBER);',
             'BOOT-INF/lib/flyway-core-1.jar': 'library',
             'BOOT-INF/classes/application.yml': 'flyway: configured'}
    files.update(changes or {})
    with zipfile.ZipFile(directory / 'backend.jar', 'w') as archive:
        for name, data in files.items():
            if data is not None:
                archive.writestr(name, data)


def child(suffix):
    values = {'ReleaseId': NEW_SHA, 'BackendImageDigest': DIGEST, 'FrontendImageDigest': DIGEST,
              'ServiceArn': 'service', 'ApplicationTaskDefinitionArn': 'definition'}
    for container, output in [('bootstrap', 'BootstrapTaskDefinitionArn'), ('migrate', 'MigrationTaskDefinitionArn')]:
        values[output] = f'arn:aws:ecs:eu-west-1:{ACCOUNT}:task-definition/delivery-lab-dev-migration-{container}:1'
    params = {'EnvironmentStackName': 'delivery-lab-dev', 'SharedStackName': 'delivery-lab-shared',
              'ReleaseId': NEW_SHA, 'FrontendImageDigest': DIGEST, 'BackendImageDigest': DIGEST, 'DesiredCount': '1'}
    return {'StackId': f'arn:aws:cloudformation:eu-west-1:{ACCOUNT}:stack/delivery-lab-dev-{suffix}/unique',
            'StackStatus': 'UPDATE_COMPLETE', 'LastUpdatedTime': '2026-09-26T10:00:00Z',
            'Tags': [{'Key': 'Project', 'Value': 'delivery-lab'},
                     {'Key': 'ParentStackId', 'Value': foundation()['StackId']}],
            'Parameters': [{'ParameterKey': k, 'ParameterValue': v} for k, v in params.items()],
            'Outputs': [{'OutputKey': k, 'OutputValue': v} for k, v in values.items()]}


def database_task(container, **changes):
    task = {'taskArn': container + '-task', 'taskDefinitionArn': recovery.outputs(child('migration'))[
            'BootstrapTaskDefinitionArn' if container == 'bootstrap' else 'MigrationTaskDefinitionArn'],
            'createdAt': '2026-09-26T10:00:00Z', 'lastStatus': 'STOPPED', 'stopCode': 'EssentialContainerExited',
            'overrides': {'containerOverrides': [{'name': container}]},
            'containers': [{'name': container, 'exitCode': 0,
                            'image': f'{ACCOUNT}.dkr.ecr.eu-west-1.amazonaws.com/delivery-lab/backend@{DIGEST}'}]}
    task.update(changes)
    return task


class DatabaseCompatibilityTests(unittest.TestCase):
    def test_unchanged_bundle_accepts_different_application_code(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / 'old', Path(directory) / 'new'
            jar(old)
            jar(new, {'BOOT-INF/classes/Controller.class': 'new application code'})
            self.assertEqual(len(recovery.require_same_migrations(old, new)), 64)

    def test_added_removed_modified_sql_engine_or_configuration_blocks_recovery(self):
        cases = [{MIGRATION: 'ALTER TABLE customers DROP COLUMN name;'},
                 {'BOOT-INF/classes/db/migration/V2__more.sql': 'ALTER TABLE customers ADD x NUMBER;'},
                 {MIGRATION: None}, {'BOOT-INF/lib/flyway-core-1.jar': 'new engine'},
                 {'BOOT-INF/classes/application.yml': 'different locations'}]
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / 'old', Path(directory) / 'new'
            jar(old)
            for change in cases:
                jar(new, change)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    recovery.require_same_migrations(old, new)

    def test_repeatable_and_callback_scripts_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name in ('R__view.sql', 'afterMigrate.sql', '../outside.sql'):
                jar(path, {'BOOT-INF/classes/db/migration/' + name: 'sql'})
                with self.subTest(name=name), self.assertRaises(ValueError):
                    recovery.migration_bundle(path / 'backend.jar')

    def test_recent_successful_database_tasks_are_required(self):
        aws = Mock(account=ACCOUNT, region='eu-west-1')
        aws.call.side_effect = [{'taskArns': []}, {'taskArns': ['bootstrap-task']}, {'tasks': [database_task('bootstrap')]},
                                {'taskArns': []}, {'taskArns': ['migrate-task']}, {'tasks': [database_task('migrate')]}]
        result = recovery.migration_task_evidence(aws, 'dev', child('migration'))
        self.assertEqual(result, {'bootstrap': 'bootstrap-task', 'migrate': 'migrate-task'})

    def test_running_missing_failed_or_overridden_database_tasks_block_recovery(self):
        aws = Mock(account=ACCOUNT, region='eu-west-1')
        bad = database_task('bootstrap')
        bad['containers'][0]['exitCode'] = 1
        overridden = database_task('bootstrap', overrides={'containerOverrides': [{'name': 'bootstrap', 'command': ['other']} ]})
        cases = [[{'taskArns': ['running']}], [{'taskArns': []}, {'taskArns': []}],
                 [{'taskArns': []}, {'taskArns': ['failed']}, {'tasks': [bad]}],
                 [{'taskArns': []}, {'taskArns': ['overridden']}, {'tasks': [overridden]}]]
        for responses in cases:
            aws.call.side_effect = responses
            with self.subTest(responses=responses), self.assertRaises(ValueError):
                recovery.migration_task_evidence(aws, 'dev', child('migration'))

    def test_newer_failed_task_cannot_be_hidden_by_older_success(self):
        aws = Mock(account=ACCOUNT, region='eu-west-1')
        failed = database_task('bootstrap', createdAt='2026-09-26T11:00:00Z', lastStatus='RUNNING')
        aws.call.side_effect = [{'taskArns': []}, {'taskArns': ['old', 'new']},
                                {'tasks': [database_task('bootstrap'), failed]}]
        with self.assertRaisesRegex(ValueError, 'did not succeed'):
            recovery.migration_task_evidence(aws, 'dev', child('migration'))


class RecoveryChangeTests(unittest.TestCase):
    def setUp(self):
        self.aws = Mock(account=ACCOUNT, region='eu-west-1', role='cloudformation-role', command=['aws'])
        self.data = receipt()
        self.app, self.migration = child('app'), child('migration')
        self.instance = recovery.Recovery(self.aws, 'dev', foundation(), self.app, self.migration,
                                          self.data, Path('/unused'))
        self.instance.unchanged = Mock()
        self.plan = {'Status': 'CREATE_COMPLETE', 'Changes': [{'ResourceChange': {
            'Action': 'Modify', 'LogicalResourceId': 'ApplicationTask',
            'ResourceType': 'AWS::ECS::TaskDefinition', 'Replacement': 'True'}}, {'ResourceChange': {
            'Action': 'Modify', 'LogicalResourceId': 'ApplicationService',
            'ResourceType': 'AWS::ECS::Service', 'Replacement': 'False'}}]}

    def test_only_release_parameters_change_and_previous_template_is_retained(self):
        self.aws.call.side_effect = [{'Id': 'change-set'}, {}, self.plan, {}]
        with patch.object(recovery, 'migration_task_evidence'), patch.object(recovery.subprocess, 'run'):
            result = self.instance.apply()
        args = self.aws.call.call_args_list[0].args
        self.assertIn('--use-previous-template', args)
        updated = json.loads(args[args.index('--parameters') + 1])
        self.assertEqual({p['ParameterKey'] for p in updated if 'ParameterValue' in p},
                         {'ReleaseId', 'FrontendImageDigest', 'BackendImageDigest'})
        self.assertFalse(result['already_configured'])
        self.assertFalse(any(call.args[:2] == ('ecs', 'run-task') for call in self.aws.call.call_args_list))

    def test_unexpected_resource_change_or_service_replacement_never_executes(self):
        for key, value in [('LogicalResourceId', 'Database'), ('Action', 'Remove'), ('Replacement', 'True')]:
            plan = copy.deepcopy(self.plan)
            plan['Changes'][1]['ResourceChange'][key] = value
            self.aws.call.reset_mock()
            self.aws.call.side_effect = [{'Id': 'change-set'}, {}, plan, {}]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'only update'):
                self.instance.apply()
            calls = [call.args[:2] for call in self.aws.call.call_args_list]
            self.assertNotIn(('cloudformation', 'execute-change-set'), calls)
            self.assertIn(('cloudformation', 'delete-change-set'), calls)

    def test_already_restored_release_is_verified_without_a_stack_update(self):
        for parameter in self.app['Parameters']:
            if parameter['ParameterKey'] == 'ReleaseId':
                parameter['ParameterValue'] = SHA
        self.assertTrue(self.instance.apply()['already_configured'])
        self.aws.call.assert_not_called()

    def test_environment_change_after_planning_stops_execution(self):
        self.instance.unchanged.side_effect = [None, ValueError('environment changed')]
        self.aws.call.side_effect = [{'Id': 'change-set'}, {}, self.plan, {}]
        with self.assertRaisesRegex(ValueError, 'environment changed'):
            self.instance.apply()
        self.assertNotIn(('cloudformation', 'execute-change-set'), [c.args[:2] for c in self.aws.call.call_args_list])

    def test_old_task_or_failed_smoke_test_cannot_verify_recovery(self):
        app = child('app')
        for output in app['Outputs']:
            if output['OutputKey'] == 'ReleaseId':
                output['OutputValue'] = SHA
        self.aws.describe.return_value = app
        self.instance.engine = Mock(values={'LoadBalancerDnsName': 'example.test'})
        self.instance.engine.verify_service.side_effect = ValueError('wrong digest')
        with self.assertRaisesRegex(ValueError, 'wrong digest'):
            self.instance.verify({})
        self.instance.engine.verify_service.side_effect = None
        with patch.object(recovery, 'smoke', side_effect=ValueError('CRUD failed')):
            with self.assertRaisesRegex(ValueError, 'CRUD failed'):
                self.instance.verify({})


class RecoveryGateTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(environment='dev', release=SHA, previous_run_id='123',
                    repository=REPOSITORY, account=ACCOUNT, region='eu-west-1', profile=None,
                    require_role=True, verify_only=False, confirm_recovery=True)

    def test_applying_requires_confirmation_before_aws_access(self):
        self.args.confirm_recovery = False
        with patch.object(recovery, 'Aws') as aws, self.assertRaisesRegex(ValueError, 'confirmation'):
            recovery.recover(self.args)
        aws.assert_not_called()

    def test_unknown_application_template_cannot_bypass_database_gate(self):
        aws = Mock()
        aws.call.return_value = {'TemplateBody': 'different template with startup SQL'}
        with self.assertRaisesRegex(ValueError, 'template differs'):
            recovery.check_application_template(aws, child('app'))
        aws.call.return_value = {'TemplateBody': (ROOT / 'infra/aws/application.yaml').read_text()}
        recovery.check_application_template(aws, child('app'))

    def test_foreign_child_or_failed_stack_is_rejected(self):
        for key, value in [('Tags', []), ('StackStatus', 'UPDATE_ROLLBACK_FAILED')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                recovery.owned_child(dict(child('app'), **{key: value}), foundation(), 'delivery-lab-dev-app')

    def test_dry_run_apply_wrong_session_and_failed_verification(self):
        for mode in ('verify', 'apply', 'wrong_session', 'smoke_failed'):
            self.args.verify_only = mode == 'verify'
            aws = Mock(account=ACCOUNT, region='eu-west-1')
            aws.verify.return_value = {'Arn': f'arn:aws:sts::{ACCOUNT}:assumed-role/delivery-lab-github-dev/session'}
            aws.describe.side_effect = [foundation(), child('app'), child('migration')]
            previous = receipt()
            if mode == 'wrong_session':
                previous['foundation_stack_id'] += '-other'
            github = Mock()
            github.evidence.return_value = (previous, {'run_id': '123'})
            engine = Mock()
            engine.apply.return_value = {'change_set': 'change-set'}
            engine.verify.return_value = {'url': 'http://example.test'}
            if mode == 'smoke_failed':
                engine.verify.side_effect = ValueError('smoke failed')
            with self.subTest(mode=mode), patch.object(recovery, 'GitHub', return_value=github), \
                    patch.object(recovery, 'current_execution', return_value={'kind': 'local'}), \
                    patch.object(recovery, 'Aws', return_value=aws), \
                    patch.object(recovery, 'check_application_template'), \
                    patch.object(recovery, 'load_release', return_value=(receipt(), Path('/unused'))), \
                    patch.object(recovery, 'find_ci', return_value='456'), \
                    patch.object(recovery, 'require_same_migrations', return_value='fingerprint'), \
                    patch.object(recovery, 'migration_task_evidence', return_value={}), \
                    patch.object(recovery, 'resolve_images', return_value={}), \
                    patch.object(recovery, 'Recovery', return_value=engine):
                if mode in ('wrong_session', 'smoke_failed'):
                    with self.assertRaises(ValueError):
                        recovery.recover(self.args)
                else:
                    result = recovery.recover(self.args)
                    self.assertEqual(result['status'], 'verified-only' if mode == 'verify' else 'recovered')
                if mode in ('verify', 'wrong_session'):
                    engine.apply.assert_not_called()

    def test_failed_recovery_invalidates_old_success_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receipt.json'
            path.write_text('{"status":"recovered"}')
            args = ['recover', '--environment', 'dev', '--release', SHA, '--previous-run-id', '123',
                    '--repository', REPOSITORY, '--account', ACCOUNT, '--output', str(path)]
            with patch.object(sys, 'argv', args), patch.object(recovery, 'recover', side_effect=ValueError('blocked')):
                with self.assertRaisesRegex(ValueError, 'blocked'):
                    recovery.main()
            self.assertEqual(json.loads(path.read_text())['status'], 'failed')

    def test_prod_recovery_requires_original_acceptance_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            github = promotion.GitHub(REPOSITORY)
            workflow = run('prod')
            previous = receipt('prod')
            previous['execution'] = promotion.execution_identity(REPOSITORY, workflow)
            previous['promotion'] = {'previous': {'run_id': '123'}, 'accepted_uat': False}
            github.run = Mock(return_value=workflow)
            def download(*args, **kwargs):
                args[3].write_text(json.dumps(previous))
                return {}
            github.artifact = Mock(side_effect=download)
            with self.assertRaisesRegex(ValueError, 'UAT acceptance'):
                github.evidence('prod', '123', SHA, Path(directory))


class MigrationPolicyTests(unittest.TestCase):
    def test_new_versions_pass_but_changed_or_removed_versions_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            def git(*args):
                subprocess.run(['git', *args], cwd=base, capture_output=True, check=True)
            git('init', '-q')
            git('config', 'user.email', 'test@example.invalid')
            git('config', 'user.name', 'Test')
            migrations = base / 'backend/src/main/resources/db/migration'
            migrations.mkdir(parents=True)
            original = migrations / 'V1__first.sql'
            original.write_text('CREATE TABLE example(id NUMBER);')
            git('add', '.')
            git('commit', '-qm', 'initial')
            second = migrations / 'V2__second.sql'
            second.write_text('ALTER TABLE example ADD name VARCHAR2(20);')
            git('add', '.')
            git('commit', '-qm', 'add version')
            script = [sys.executable, str(ROOT / 'scripts/check_migration_changes.py')]
            self.assertEqual(subprocess.run(script, cwd=base, capture_output=True).returncode, 0)
            original.write_text('changed existing migration')
            git('add', '.')
            git('commit', '-qm', 'bad edit')
            self.assertNotEqual(subprocess.run(script, cwd=base, capture_output=True).returncode, 0)
            original.unlink()
            git('add', '.')
            git('commit', '-qm', 'bad deletion')
            self.assertNotEqual(subprocess.run(script, cwd=base, capture_output=True).returncode, 0)


class DiagnosticTests(unittest.TestCase):
    def test_diagnostics_are_read_only_and_exclude_connection_and_secret_details(self):
        aws = Mock()
        stack = foundation()
        stack['Outputs'] += [{'OutputKey': 'DatabaseUrl', 'OutputValue': 'private-connection'},
                             {'OutputKey': 'ApplicationDatabaseSecretArn', 'OutputValue': 'secret-arn'}]
        aws.describe.side_effect = [stack, None, None]
        result = aws_diagnostics.collect(aws, 'dev')
        text = json.dumps(result)
        self.assertNotIn('private-connection', text)
        self.assertNotIn('secret-arn', text)
        aws.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
