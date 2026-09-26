#!/usr/bin/env python3
"""Collect bounded deployment state without reading secrets or container log contents."""
import argparse
import subprocess
import sys
from aws_deployment import Aws, ENVIRONMENTS, PROJECT, outputs
from lab import now, write_json


def collect(aws, environment):
    report = {'environment': environment, 'captured_at': now(), 'stacks': {}, 'errors': []}
    name = f'{PROJECT}-{environment}'
    for suffix in ('', '-migration', '-app'):
        try:
            stack = aws.describe(name + suffix)
            if not stack:
                report['stacks'][name + suffix] = {'status': 'absent'}
                continue
            item = {'id': stack['StackId'], 'status': stack['StackStatus'], 'outputs': outputs(stack)}
            # Exclude database connection details and secret identifiers from the diagnostic artifact.
            item['outputs'] = {key: value for key, value in item['outputs'].items()
                               if key in {'ReleaseId', 'FrontendImageDigest', 'BackendImageDigest',
                                          'ExpiresAt', 'ServiceArn', 'ApplicationTaskDefinitionArn'}}
            if suffix:
                events = aws.call('cloudformation', 'describe-stack-events', '--stack-name', name + suffix,
                                  '--max-items', '15')['StackEvents']
                item['events'] = [{k: event.get(k) for k in ('Timestamp', 'LogicalResourceId', 'ResourceStatus',
                                                            'ResourceStatusReason')} for event in events]
            report['stacks'][name + suffix] = item
            if suffix == '-app' and outputs(stack).get('ServiceArn'):
                service = aws.call('ecs', 'describe-services', '--cluster', name,
                                   '--services', outputs(stack)['ServiceArn'])
                report['services'] = [{key: value for key, value in row.items()
                                       if key in {'serviceArn', 'status', 'desiredCount', 'runningCount',
                                                  'pendingCount', 'taskDefinition', 'deployments', 'events'}}
                                      for row in service.get('services', [])]
                for row in report['services']:
                    row['events'] = row.get('events', [])[:10]
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            report['errors'].append({'stack': name + suffix, 'error': str(error)[:500]})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=ENVIRONMENTS, required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', choices=['eu-west-1'], default='eu-west-1')
    parser.add_argument('--profile')
    parser.add_argument('--require-role', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    aws = Aws(args.account, args.region, args.profile)
    identity = aws.verify()
    if args.require_role and not identity['Arn'].startswith(
            f'arn:aws:sts::{args.account}:assumed-role/{PROJECT}-github-{args.environment}/'):
        raise ValueError('Diagnostics require the temporary GitHub environment role.')
    write_json(args.output, collect(aws, args.environment))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
