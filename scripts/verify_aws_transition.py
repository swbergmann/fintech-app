#!/usr/bin/env python3
"""Audit a release's GitHub evidence and live AWS environments without deploying."""
import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from aws_deployment import (Aws, Deployment, PROJECT, check_foundation,
                            outputs, resolve_images, validate_release)
from lab import now, write_json
from promote_aws_release import GitHub, same_release, verify_previous_environment
from smoke import request


def check_isolation(foundations):
    """Compare resource identities without exposing secret identifiers in the report."""
    unique = ('ClusterArn', 'DatabaseInstanceIdentifier', 'ApplicationDatabaseSecretArn',
              'DatabaseAdminSecretArn', 'TaskSecurityGroupId', 'LoadBalancerDnsName')
    values = [outputs(stack) for stack in foundations]
    for key in unique:
        items = [value[key] for value in values]
        if any(not item for item in items) or len(set(items)) != len(items):
            raise ValueError(f'Environments share or omit {key}.')
    subnets = [set(value['TaskSubnetIds'].split(',')) for value in values]
    if any(left & right for index, left in enumerate(subnets) for right in subnets[index + 1:]):
        raise ValueError('Environments share application subnets.')
    return {'distinct_resources': list(unique), 'disjoint_application_subnets': True}


def audit(args):
    if not re.fullmatch(r'[0-9a-f]{40}', args.release):
        raise ValueError('Provide the full release SHA.')
    if args.prod_run and not args.uat_run:
        raise ValueError('A PROD audit requires its UAT run as well as DEV.')
    runs = [('dev', args.dev_run), ('uat', args.uat_run), ('prod', args.prod_run)]
    runs = [(environment, run) for environment, run in runs if run]
    github, aws = GitHub(args.repository), Aws(args.account, args.region, args.profile)
    aws.verify()
    with tempfile.TemporaryDirectory(prefix='delivery-lab-audit-') as temporary:
        directory = Path(temporary)
        receipts, references = {}, {}
        for environment, run in runs:
            receipt, reference = github.evidence(environment, run, args.release, directory)
            if receipts:
                prior = 'dev' if environment == 'uat' else 'uat'
                same_release(receipts[prior], receipt)
                if receipt['promotion']['previous'] != references[prior]:
                    raise ValueError('Selected runs do not form one unchanged promotion chain.')
            receipts[environment], references[environment] = receipt, reference

        ci_run = receipts['dev']['source']['ci_run_id']
        ci = github.run(ci_run, 'ci', args.release)
        archive, metadata = directory / 'release.tar.gz', directory / 'aws-release.json'
        github.artifact(ci, f'release-{args.release}', 'release.tar.gz', archive)
        github.artifact(ci, f'aws-release-{args.release}', 'aws-release.json', metadata)
        data = validate_release(archive, metadata, directory / 'release', args.account, args.region,
                                args.release, args.repository, ci_run)
        same_release(receipts['dev'], data)
        runtime = resolve_images(aws, data)
        environments, foundations = {}, []
        for environment, _ in runs:
            receipt = receipts[environment]
            verify_previous_environment(aws, receipt)
            foundation = aws.describe(f'{PROJECT}-{environment}')
            values = check_foundation(foundation, aws.account, environment=environment)
            engine = Deployment(aws, foundation, directory / 'release', data, environment)
            tasks = engine.verify_service(receipt['service_arn'], receipt['task_definition_arn'], runtime)
            url = 'http://' + values['LoadBalancerDnsName']
            _, health = request(url, '/api/health')
            _, backend = request(url, '/api/meta')
            _, frontend = request(url, '/release.json')
            if (health.get('status') != 'UP' or frontend.get('release') != args.release
                    or backend != {'environment': environment.upper(), 'release': args.release}):
                raise ValueError(f'{environment.upper()}: HTTP identity/health differs from the verified release.')
            foundations.append(foundation)
            environments[environment] = {
                'evidence': references[environment], 'url': url, 'expires_at': values['ExpiresAt'],
                'foundation_stack_id': foundation['StackId'], 'task_definition_arn': receipt['task_definition_arn'],
                'running_task_arns': tasks, 'http_identity_and_health': 'passed',
                'human_uat_accepted': receipt.get('promotion', {}).get('accepted_uat', False)}
        isolation = check_isolation(foundations) if len(foundations) > 1 else {'status': 'not-compared'}
        # A successful DEV/UAT audit must never be described as a completed PROD transition.
        return {'status': 'passed', 'scope': list(environments),
                'all_three_environments_verified': set(environments) == {'dev', 'uat', 'prod'},
                'release': args.release, 'source': data['source'], 'images': data['images'],
                'runtime_image_digests': runtime, 'environments': environments, 'isolation': isolation,
                'completed_at': now(), 'read_only': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True)
    parser.add_argument('--dev-run', required=True)
    parser.add_argument('--uat-run')
    parser.add_argument('--prod-run')
    parser.add_argument('--repository', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', choices=['eu-west-1'], default='eu-west-1')
    parser.add_argument('--profile')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = {'status': 'failed', 'release': args.release}
    write_json(args.output, result)
    try:
        result = audit(args)
        print('Verified live environments: ' + ', '.join(result['scope']).upper())
        print('All three environments verified: ' + str(result['all_three_environments_verified']))
    except Exception as error:
        result['error'] = str(error)[:1000]
        raise
    finally:
        write_json(args.output, result)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
