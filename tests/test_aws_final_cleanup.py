"""Destructive cleanup must stay scoped, wait for completion and fail honestly."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra/aws'))
import final_cleanup as cleanup
import install_final_cleanup as installer

ACCOUNT = '123456789012'


def stack(name):
    return {'StackName': name, 'StackId': f'arn:aws:cloudformation:eu-west-1:{ACCOUNT}:stack/{name}/unique',
            'StackStatus': 'CREATE_COMPLETE',
            'Tags': [{'Key': 'Project', 'Value': 'delivery-lab'}, {'Key': 'Purpose', 'Value': 'academic-lab'}]}


def empty():
    return {key: [] for key in ('repositories', 'snapshots', 'backups', 'secrets', 'logs', 'schedules', 'functions', 'blockers')}


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.aws = cleanup.Aws(ACCOUNT)
        self.aws.call = Mock()
        self.aws.describe = Mock(return_value=None)
        self.engine = cleanup.FinalCleanup(self.aws)

    def inventory_responses(self):
        values = {
            ('rds', 'describe-db-snapshots'): {'DBSnapshots': []},
            ('rds', 'describe-db-instance-automated-backups'): {'DBInstanceAutomatedBackups': []},
            ('rds', 'describe-db-instances'): {'DBInstances': []},
            ('secretsmanager', 'list-secrets'): {'SecretList': []},
            ('logs', 'describe-log-groups'): {'logGroups': []},
            ('scheduler', 'list-schedules'): {'Schedules': []},
            ('ecs', 'list-clusters'): {'clusterArns': []},
            ('elbv2', 'describe-load-balancers'): {'LoadBalancers': []},
            ('ec2', 'describe-vpcs'): {'Vpcs': []},
            ('ec2', 'describe-addresses'): {'Addresses': []},
            ('resourcegroupstaggingapi', 'get-resources'): {'ResourceTagMappingList': []},
        }
        def call(*args):
            if args[:2] == ('ecr', 'describe-repositories'):
                raise subprocess.CalledProcessError(1, ['aws'], stderr='RepositoryNotFoundException')
            if args[:2] == ('lambda', 'get-function'):
                raise subprocess.CalledProcessError(1, ['aws'], stderr='ResourceNotFoundException')
            return copy.deepcopy(values[args[:2]])
        self.aws.call.side_effect = call
        return values

    def test_empty_regional_inventory_and_unrelated_resources_are_not_deleted(self):
        values = self.inventory_responses()
        values[('rds', 'describe-db-snapshots')]['DBSnapshots'] = [{
            'DBInstanceIdentifier': 'other-db', 'DBSnapshotIdentifier': 'other-snapshot',
            'TagList': [{'Key': 'Project', 'Value': 'other'}]}]
        values[('rds', 'describe-db-instance-automated-backups')]['DBInstanceAutomatedBackups'] = [{
            'DBInstanceIdentifier': 'other-db', 'DBInstanceAutomatedBackupsArn': 'other-backup'}]
        values[('logs', 'describe-log-groups')]['logGroups'] = [{'logGroupName': '/aws/lambda/delivery-lab-cleanup-other'}]
        self.assertEqual(self.engine.inventory(), empty())
        self.assertFalse(any(call.args[1].startswith('delete-') for call in self.aws.call.call_args_list))

    def test_snapshot_needs_both_project_ownership_and_expected_database(self):
        for database, project in (('delivery-lab-dev', 'other'), ('other-db', 'delivery-lab')):
            values = self.inventory_responses()
            values[('rds', 'describe-db-snapshots')]['DBSnapshots'] = [{
                'DBInstanceIdentifier': database, 'DBSnapshotIdentifier': 'candidate',
                'TagList': [{'Key': 'Project', 'Value': project}]}]
            with self.subTest(database=database), self.assertRaisesRegex(ValueError, 'attribute snapshot'):
                self.engine.inventory()

    def test_secret_with_matching_name_but_missing_ownership_is_rejected(self):
        values = self.inventory_responses()
        values[('secretsmanager', 'list-secrets')]['SecretList'] = [{
            'Name': 'delivery-lab/dev/app-db', 'ARN': 'secret', 'Tags': []}]
        with self.assertRaisesRegex(ValueError, 'attribute secret'):
            self.engine.inventory()

    def test_tagged_unexpected_billable_resources_are_reported_as_blockers(self):
        values = self.inventory_responses()
        arn = f'arn:aws:ec2:eu-west-1:{ACCOUNT}:volume/vol-extra'
        values[('resourcegroupstaggingapi', 'get-resources')]['ResourceTagMappingList'] = [{'ResourceARN': arn}]
        self.assertEqual(self.engine.inventory()['blockers'], [arn])

    def test_deleting_snapshot_is_not_deleted_again_on_retry(self):
        resources = empty()
        resources['snapshots'] = ['snapshot']
        self.aws.call.return_value = {'DBSnapshots': [{'Status': 'deleting'}]}
        self.engine.remove_retained(resources)
        self.assertFalse(any(call.args[1] == 'delete-db-snapshot' for call in self.aws.call.call_args_list))

    def test_foreign_account_and_unowned_or_similarly_named_stacks_are_rejected(self):
        for change in ('account', 'tags', 'name'):
            value = stack('delivery-lab-dev')
            if change == 'account':
                value['StackId'] = value['StackId'].replace(ACCOUNT, '999999999999')
            elif change == 'tags':
                value['Tags'] = []
            else:
                value['StackName'] = 'delivery-lab-dev-other-project'
            with self.subTest(change=change), self.assertRaises(ValueError):
                cleanup.owned_stack(value, ACCOUNT, 'eu-west-1')

    def test_wrong_confirmation_does_not_call_aws_and_invalidates_previous_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            output.write_text('{"status":"passed"}')
            with patch.object(sys, 'argv', ['cleanup', '--account', ACCOUNT, '--confirm', 'yes', '--output', str(output)]), \
                    patch.object(cleanup, 'Aws') as aws, self.assertRaises(ValueError):
                cleanup.main()
            aws.assert_not_called()
            self.assertEqual(json.loads(output.read_text())['status'], 'failed')

    def test_api_denial_is_not_treated_as_an_absent_repository(self):
        self.aws.call.side_effect = subprocess.CalledProcessError(1, ['aws'], stderr='AccessDeniedException')
        with self.assertRaises(subprocess.CalledProcessError):
            self.engine.optional(('ecr', 'describe-repositories'), 'RepositoryNotFoundException')

    def test_child_with_wrong_parent_blocks_all_deletion(self):
        parent, child = stack('delivery-lab-dev'), stack('delivery-lab-dev-app')
        child['Tags'].append({'Key': 'ParentStackId', 'Value': 'another-stack'})
        self.aws.describe.side_effect = lambda name: {parent['StackName']: parent, child['StackName']: child}.get(name)
        with self.assertRaisesRegex(ValueError, 'ParentStackId'):
            self.engine.retire()
        self.aws.call.assert_not_called()

    def test_waits_for_actual_stack_deletion(self):
        value = stack('delivery-lab-dev')
        self.aws.describe.side_effect = [dict(value, StackStatus='DELETE_IN_PROGRESS'),
                                         dict(value, StackStatus='DELETE_COMPLETE')]
        with patch.object(cleanup.time, 'sleep') as sleep:
            self.engine.delete_stack(value, self.aws.role)
        sleep.assert_called_once()
        self.assertEqual(self.engine.deleted, ['delivery-lab-dev'])

    def test_delete_failed_never_passes_and_in_progress_is_not_deleted_twice(self):
        value = dict(stack('delivery-lab-dev'), StackStatus='DELETE_IN_PROGRESS')
        self.aws.describe.return_value = dict(value, StackStatus='DELETE_FAILED')
        with self.assertRaisesRegex(ValueError, 'deletion failed'):
            self.engine.delete_stack(value, self.aws.role)
        self.aws.call.assert_not_called()
        self.assertEqual(self.engine.deleted, [])

    def test_busy_stack_is_not_deleted(self):
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.engine.delete_stack(dict(stack('delivery-lab-dev'), StackStatus='UPDATE_IN_PROGRESS'), self.aws.role)
        self.aws.call.assert_not_called()

    def test_ownership_error_happens_before_any_deletion(self):
        self.engine.stacks = Mock(return_value={})
        self.engine.inventory = Mock(side_effect=ValueError('unowned repository'))
        self.engine.delete_stack = Mock()
        with self.assertRaisesRegex(ValueError, 'unowned'):
            self.engine.retire()
        self.engine.delete_stack.assert_not_called()

    def test_cleanup_order_and_retry_after_everything_is_already_absent(self):
        for already_absent in (False, True):
            with self.subTest(already_absent=already_absent):
                engine = cleanup.FinalCleanup(self.aws)
                names = ('delivery-lab-dev-app', 'delivery-lab-dev-migration', 'delivery-lab-dev',
                         'delivery-lab-shared', 'delivery-lab-access')
                stacks = {} if already_absent else {name: stack(name) for name in names}
                engine.stacks = Mock(side_effect=[stacks, {}])
                engine.inventory = Mock(return_value=empty())
                engine.delete_stack = Mock()
                engine.stop_tasks = Mock()
                engine.remove_retained = Mock()
                self.aws.call.return_value = {'TemplateBody': {'Resources': {'GitHubProvider': {'DeletionPolicy': 'Retain'}}}}
                engine.retire()
                self.assertEqual([call.args[0]['StackName'] for call in engine.delete_stack.call_args_list],
                                 [] if already_absent else list(names))
                if not already_absent:
                    self.assertEqual(engine.delete_stack.call_args.args[1], engine.cleanup_role)

    def test_old_access_template_cannot_delete_shared_github_identity(self):
        self.engine.stacks = Mock(return_value={'delivery-lab-access': stack('delivery-lab-access')})
        self.engine.inventory = Mock(return_value=empty())
        self.aws.call.return_value = {'TemplateBody': {'Resources': {'GitHubProvider': {'Type': 'AWS::IAM::OIDCProvider'}}}}
        with self.assertRaisesRegex(ValueError, 'identity provider'):
            self.engine.retire()
        self.assertFalse(any(call.args[:2] == ('cloudformation', 'delete-stack') for call in self.aws.call.call_args_list))

    def test_retained_repository_deletion_removes_its_images(self):
        resources = empty()
        resources['repositories'] = ['delivery-lab/backend']
        self.engine.remove_retained(resources)
        self.aws.call.assert_called_once_with('ecr', 'delete-repository', '--repository-name', 'delivery-lab/backend', '--force')

    def test_remaining_resources_prevent_success(self):
        self.engine.stacks = Mock(return_value={})
        resources = empty()
        resources['blockers'] = ['unexpected-project-instance']
        self.engine.inventory = Mock(return_value=resources)
        self.engine.remove_retained = Mock()
        with patch.object(cleanup.time, 'monotonic', side_effect=[0, 601]), self.assertRaises(TimeoutError):
            self.engine.retire()

    def test_real_access_template_retains_provider(self):
        self.assertTrue(cleanup.provider_is_retained((ROOT / 'infra/aws/access.yaml').read_text()))
        self.assertFalse(cleanup.provider_is_retained('Resources:\n  GitHubProvider:\n    Type: AWS::IAM::OIDCProvider\n'))

    def test_install_only_updates_access_and_installs_a_free_iam_role(self):
        self.aws.verify = Mock()
        access = stack('delivery-lab-access')
        access['Parameters'] = [{'ParameterKey': 'GitHubSubjectPrefix', 'ParameterValue': 'repo:owner/repo'}]
        self.aws.describe.return_value = access
        with patch.object(installer.subprocess, 'run') as run:
            installer.install(self.aws)
        names = [call.args[0][call.args[0].index('--stack-name') + 1] for call in run.call_args_list]
        self.assertEqual(names, ['delivery-lab-access', 'delivery-lab-final-cleanup'])


if __name__ == '__main__':
    unittest.main()
