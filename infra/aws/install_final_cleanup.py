#!/usr/bin/env python3
"""Install final-cleanup IAM access once, using the account administrator profile.

Updates only access policies/templates; never creates an environment or runs cleanup.
"""
import argparse
import subprocess

from provision import Aws, PROJECT, ROOT
from final_cleanup import owned_stack


def install(aws):
    aws.verify()
    access = aws.describe(f'{PROJECT}-access')
    if not access:
        raise ValueError('Bootstrap the access stack first using infra/aws/README.md.')
    owned_stack(access, aws.account, aws.region)
    if access['StackStatus'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'):
        raise ValueError('Access stack is not ready for an update.')
    parameters = {item['ParameterKey']: item['ParameterValue'] for item in access['Parameters']}
    provider = parameters.get('ExistingOidcProviderArn') or (
        f'arn:aws:iam::{aws.account}:oidc-provider/token.actions.githubusercontent.com')
    # Do not use the infrastructure service role: only the administrator can
    # bootstrap/update the access stack and this separate, narrowly scoped role.
    for name, template, values in (
        (f'{PROJECT}-access', 'access.yaml', parameters),
        (f'{PROJECT}-final-cleanup', 'final-cleanup.yaml',
         {'GitHubSubjectPrefix': parameters['GitHubSubjectPrefix'], 'GitHubProviderArn': provider}),
    ):
        subprocess.run(aws.command + ['cloudformation', 'deploy', '--stack-name', name,
                       '--template-file', str(ROOT / template), '--capabilities', 'CAPABILITY_NAMED_IAM',
                       '--no-fail-on-empty-changeset', '--tags', f'Project={PROJECT}', 'Purpose=academic-lab',
                       '--parameter-overrides', *[f'{key}={value}' for key, value in values.items()]], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='eu-west-1', choices=['eu-west-1'])
    parser.add_argument('--profile', default='default')
    args = parser.parse_args()
    install(Aws(args.account, args.region, args.profile))
