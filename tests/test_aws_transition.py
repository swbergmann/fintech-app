"""The final audit must not mistake partial, stale or shared environments for success."""
import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import verify_aws_transition as transition
from test_aws_deployment import ACCOUNT, SHA, foundation
from test_aws_promotion import REPOSITORY, receipt


def isolated(environment):
    stack = foundation()
    stack['StackId'] += '-' + environment
    fields = ('ClusterArn', 'DatabaseInstanceIdentifier', 'ApplicationDatabaseSecretArn',
              'DatabaseAdminSecretArn', 'TaskSecurityGroupId', 'LoadBalancerDnsName', 'TaskSubnetIds')
    values = transition.outputs(stack)
    values.update({key: environment + '-' + key for key in fields})
    stack['Outputs'] = [{'OutputKey': key, 'OutputValue': value} for key, value in values.items()]
    return stack


class TransitionTests(unittest.TestCase):
    def test_isolation_rejects_shared_database_secret_and_subnet(self):
        for key in ('DatabaseInstanceIdentifier', 'ApplicationDatabaseSecretArn', 'TaskSubnetIds'):
            dev, uat = isolated('dev'), isolated('uat')
            for item in uat['Outputs']:
                if item['OutputKey'] == key:
                    item['OutputValue'] = transition.outputs(dev)[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                transition.check_isolation([dev, uat])

    def test_isolation_report_omits_private_resource_identifiers(self):
        report = transition.check_isolation([isolated('dev'), isolated('uat'), isolated('prod')])
        self.assertTrue(report['disjoint_application_subnets'])
        self.assertNotIn('dev-ApplicationDatabaseSecretArn', json.dumps(report))

    def test_prod_cannot_be_audited_without_uat(self):
        args = argparse.Namespace(release=SHA, dev_run='1', uat_run=None, prod_run='3')
        with patch.object(transition, 'Aws') as aws, self.assertRaisesRegex(ValueError, 'requires its UAT'):
            transition.audit(args)
        aws.assert_not_called()

    def test_partial_full_and_mismatched_promotion_chain(self):
        for scope in ('partial', 'full', 'mismatch', 'runtime_failure', 'network_failure'):
            environments = ('dev', 'uat', 'prod') if scope == 'full' else ('dev', 'uat')
            args = argparse.Namespace(release=SHA, dev_run='1', uat_run='2',
                    prod_run='3' if scope == 'full' else None, account=ACCOUNT,
                    region='eu-west-1', profile=None, repository=REPOSITORY)
            receipts = {env: receipt(env) for env in environments}
            references = {env: {'run_id': str(index + 1)} for index, env in enumerate(environments)}
            for env, prior in [('uat', 'dev'), ('prod', 'uat')]:
                if env in receipts:
                    receipts[env]['promotion'] = {'previous': references[prior], 'accepted_uat': env == 'prod'}
            if scope == 'mismatch':
                receipts['uat']['promotion']['previous'] = {'run_id': '99'}
            github = Mock()
            github.evidence.side_effect = [(receipts[env], references[env]) for env in environments]
            aws = Mock(account=ACCOUNT)
            aws.describe.side_effect = [isolated(env) for env in environments]
            engine = Mock()
            engine.verify_service.return_value = ['task']
            if scope == 'runtime_failure':
                engine.verify_service.side_effect = ValueError('wrong running image')
            responses = []
            for env in environments:
                responses.extend([(200, {'status': 'UP'}), (200, {'environment': env.upper(), 'release': SHA}),
                                  (200, {'release': SHA})])
            with self.subTest(scope=scope), patch.object(transition, 'GitHub', return_value=github), \
                    patch.object(transition, 'Aws', return_value=aws), \
                    patch.object(transition, 'validate_release', return_value=copy.deepcopy(receipts['dev'])), \
                    patch.object(transition, 'resolve_images', return_value={'backend': 'digest', 'frontend': 'digest'}), \
                    patch.object(transition, 'verify_previous_environment'), \
                    patch.object(transition, 'check_foundation', side_effect=lambda stack, *a, **k: transition.outputs(stack)), \
                    patch.object(transition, 'check_runner_network',
                                 side_effect=ValueError('client address changed') if scope == 'network_failure' else None), \
                    patch.object(transition, 'Deployment', return_value=engine), \
                    patch.object(transition, 'request', side_effect=responses):
                if scope in ('mismatch', 'runtime_failure', 'network_failure'):
                    with self.assertRaises(ValueError):
                        transition.audit(args)
                    if scope == 'network_failure':
                        engine.verify_service.assert_not_called()
                else:
                    result = transition.audit(args)
                    self.assertEqual(result['all_three_environments_verified'], scope == 'full')
                    self.assertEqual(result['scope'], list(environments))
                    self.assertTrue(result['read_only'])
                    engine.deploy.assert_not_called()

    def test_failed_audit_replaces_a_previous_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audit.json'
            path.write_text('{"status":"passed"}')
            argv = ['verify', '--release', SHA, '--dev-run', '1', '--repository', REPOSITORY,
                    '--account', ACCOUNT, '--output', str(path)]
            with patch.object(sys, 'argv', argv), patch.object(transition, 'audit', side_effect=ValueError('stale evidence')):
                with self.assertRaisesRegex(ValueError, 'stale evidence'):
                    transition.main()
            self.assertEqual(json.loads(path.read_text())['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
