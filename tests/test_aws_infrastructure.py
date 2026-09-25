"""Safety tests for disposable infrastructure; no AWS calls or credentials."""
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('provision', ROOT / 'infra/aws/provision.py')
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)


def cleanup_module(client):
    # Extract the actual inline Lambda, without requiring YAML dependencies in CI.
    template = (ROOT / 'infra/aws/access.yaml').read_text()
    block = template.split('        ZipFile: |\n', 1)[1].split('\n  CleanupRetry:', 1)[0]
    code = '\n'.join(line[10:] for line in block.splitlines())
    boto3 = types.ModuleType('boto3')
    boto3.client = Mock(return_value=client)
    exceptions = types.ModuleType('botocore.exceptions')
    exceptions.ClientError = type('ClientError', (Exception,), {})
    namespace = {}
    with patch.dict(sys.modules, {'boto3': boto3, 'botocore.exceptions': exceptions}):
        exec(compile(code, 'access.yaml:CleanupFunction', 'exec'), namespace)
    return namespace


class ProvisionSafetyTests(unittest.TestCase):
    def test_wrong_account_fails_before_mutation(self):
        aws = provision.Aws('123456789012')
        aws.call = Mock(return_value={'Account': '999999999999', 'Arn': 'wrong'})
        with self.assertRaisesRegex(ValueError, 'does not match'):
            aws.verify()
        aws.call.assert_called_once_with('sts', 'get-caller-identity')

    def test_cidr_rejects_broad_private_and_ipv6_networks(self):
        for value in ('0.0.0.0/0', '8.8.8.0/24', '127.0.0.1/32', '10.1.1.1/32', '::1/128'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                provision.client_cidr(value)
        self.assertEqual(provision.client_cidr('8.8.8.8/32'), '8.8.8.8/32')

    def test_expiry_is_eight_hours(self):
        before = datetime.now(timezone.utc) + timedelta(hours=8)
        value = datetime.fromisoformat(provision.expiry()).replace(tzinfo=timezone.utc)
        self.assertLess(abs((value - before).total_seconds()), 2)

    def test_unowned_stack_cannot_be_deleted(self):
        aws = provision.Aws('123456789012')
        aws.describe = Mock(return_value={'StackId': 'existing', 'Tags': []})
        aws.call = Mock()
        with self.assertRaisesRegex(ValueError, 'ownership'):
            aws.delete()
        aws.call.assert_not_called()


class CleanupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.module = cleanup_module(self.client)
        self.stack_id = 'arn:aws:cloudformation:eu-west-1:123456789012:stack/delivery-lab-dev/unique'
        self.stack = {'StackId': self.stack_id, 'StackStatus': 'CREATE_COMPLETE',
                      'Parameters': [{'ParameterKey': 'ExpiresAt', 'ParameterValue': '2020-01-01T00:00:00'}]}
        self.context = Mock()
        self.context.get_remaining_time_in_millis.return_value = 900000
        self.variables = patch.dict(os.environ, PROJECT='delivery-lab', ACCOUNT='123456789012')
        self.variables.start()
        self.addCleanup(self.variables.stop)

    def run_cleanup(self, event=None):
        return self.module['handler'](event or {'environment': 'dev', 'stack_id': self.stack_id}, self.context)

    def test_recreated_environment_is_not_deleted_by_old_schedule(self):
        self.module['describe'] = Mock(return_value=self.stack)
        self.run_cleanup({'environment': 'dev', 'stack_id': 'old-stack'})
        self.client.delete_stack.assert_not_called()

    def test_invalid_environment_rejected(self):
        with self.assertRaises(ValueError):
            self.run_cleanup({'environment': 'other', 'stack_id': self.stack_id})
        self.client.delete_stack.assert_not_called()

    def test_wrong_account_rejected(self):
        self.module['describe'] = Mock(return_value=self.stack)
        with patch.dict(os.environ, ACCOUNT='999999999999'), self.assertRaises(ValueError):
            self.run_cleanup()
        self.client.delete_stack.assert_not_called()

    def test_unowned_child_blocks_cleanup(self):
        self.module['describe'] = Mock(side_effect=[self.stack, {'StackId': 'child', 'Tags': []}])
        with self.assertRaisesRegex(ValueError, 'ParentStackId'):
            self.run_cleanup()
        self.client.delete_stack.assert_not_called()

    def test_early_or_obsolete_expiry_event_cannot_delete_live_session(self):
        self.stack['Parameters'][0]['ParameterValue'] = provision.expiry()
        self.module['describe'] = Mock(return_value=self.stack)
        self.assertEqual(self.run_cleanup()['status'], 'not expired')
        self.client.delete_stack.assert_not_called()

    def test_deletion_in_progress_is_not_restarted(self):
        self.stack['StackStatus'] = 'DELETE_IN_PROGRESS'
        self.module['describe'] = Mock(return_value=self.stack)
        self.assertEqual(self.run_cleanup()['status'], 'deletion already in progress')
        self.client.delete_stack.assert_not_called()

    def test_owned_children_deleted_before_foundation(self):
        app = {'StackId': 'app', 'Tags': [{'Key': 'ParentStackId', 'Value': self.stack_id}]}
        migration = {'StackId': 'migration', 'Tags': app['Tags']}
        self.module['describe'] = Mock(side_effect=[self.stack, app, None, migration, None])
        self.run_cleanup()
        self.assertEqual([call.kwargs['StackName'] for call in self.client.delete_stack.call_args_list],
                         ['app', 'migration', self.stack_id])


if __name__ == '__main__':
    unittest.main()
