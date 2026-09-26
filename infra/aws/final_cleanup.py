#!/usr/bin/env python3
"""Permanently retire this lab in eu-west-1, including retained billable data.

Uses AWS CLI pagination, scoped names and ownership tags. No database values,
credentials or log contents are read. Failures never become a successful audit.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from provision import Aws, ENVIRONMENTS, PROJECT

CONFIRMATION = 'DELETE delivery-lab'
FOUNDATIONS = tuple(f'{PROJECT}-{env}' for env in ENVIRONMENTS)
STACKS = tuple(name + suffix for name in FOUNDATIONS for suffix in ('-app', '-migration', ''))
STACKS += (f'{PROJECT}-shared', f'{PROJECT}-access')
REPOSITORIES = (f'{PROJECT}/frontend', f'{PROJECT}/backend')
LOG_PREFIX = f'/delivery-lab/{PROJECT}/'
CLEANUP_LOG = f'/aws/lambda/{PROJECT}-cleanup'


def tags(items):
    return {item.get('Key', item.get('key')): item.get('Value', item.get('value')) for item in items}


def owned_stack(stack, account, region):
    name = stack['StackName']
    identity = f'arn:aws:cloudformation:{region}:{account}:stack/{name}/'
    labels = tags(stack.get('Tags', []))
    if (name not in STACKS or not stack['StackId'].startswith(identity)
            or labels.get('Project') != PROJECT or labels.get('Purpose') != 'academic-lab'):
        raise ValueError(f'Refusing to delete unowned stack {name}.')
    return stack


def provider_is_retained(template):
    if isinstance(template, str):
        try:
            template = json.loads(template)
        except json.JSONDecodeError:
            block = re.search(r'(?ms)^  GitHubProvider:\n(.*?)(?=^  \S|\Z)', template)
            return bool(block and re.search(r'^    DeletionPolicy: Retain\s*$', block[1], re.M))
    return template.get('Resources', {}).get('GitHubProvider', {}).get('DeletionPolicy') == 'Retain'


class FinalCleanup:
    def __init__(self, aws):
        self.aws = aws
        self.cleanup_role = f'arn:aws:iam::{aws.account}:role/{PROJECT}-final-cleanup'
        self.deleted = []
        self.remaining = None

    def optional(self, args, missing):
        try:
            return self.aws.call(*args)
        except subprocess.CalledProcessError as error:
            if missing in (error.stderr or ''):
                return None
            raise

    def stacks(self):
        result = {}
        for name in STACKS:
            stack = self.aws.describe(name)
            if stack and stack['StackStatus'] != 'DELETE_COMPLETE':
                result[name] = owned_stack(stack, self.aws.account, self.aws.region)
        for name, stack in result.items():
            if name.endswith(('-app', '-migration')):
                parent_name = name.rsplit('-', 1)[0]
                parent = result.get(parent_name)
                parent_id = tags(stack['Tags']).get('ParentStackId', '')
                expected = (parent['StackId'] if parent else
                            f'arn:aws:cloudformation:{self.aws.region}:{self.aws.account}:stack/{parent_name}/')
                if not parent_id or (parent_id != expected if parent else not parent_id.startswith(expected)):
                    raise ValueError(f'{name}: child has the wrong ParentStackId.')
        return result

    def inventory(self):
        """Inspect services used by this lab; never treat an API error as absence."""
        result = {'repositories': [], 'snapshots': [], 'backups': [], 'secrets': [],
                  'logs': [], 'schedules': [], 'functions': [], 'blockers': []}
        for name in REPOSITORIES:
            value = self.optional(('ecr', 'describe-repositories', '--repository-names', name),
                                  'RepositoryNotFoundException')
            if value:
                repo = value['repositories'][0]
                labels = tags(self.aws.call('ecr', 'list-tags-for-resource',
                                             '--resource-arn', repo['repositoryArn'])['tags'])
                if labels.get('Project') != PROJECT:
                    raise ValueError(f'{name}: repository ownership is missing.')
                result['repositories'].append(name)
        for snapshot in self.aws.call('rds', 'describe-db-snapshots', '--snapshot-type', 'manual')['DBSnapshots']:
            labels = tags(snapshot.get('TagList', []))
            if not labels:
                labels = tags(self.aws.call('rds', 'list-tags-for-resource',
                                             '--resource-name', snapshot['DBSnapshotArn'])['TagList'])
            if snapshot['DBInstanceIdentifier'] in FOUNDATIONS or labels.get('Project') == PROJECT:
                if labels.get('Project') != PROJECT or snapshot['DBInstanceIdentifier'] not in FOUNDATIONS:
                    raise ValueError(f"Cannot attribute snapshot {snapshot['DBSnapshotIdentifier']} safely.")
                result['snapshots'].append(snapshot['DBSnapshotIdentifier'])
        for backup in self.aws.call('rds', 'describe-db-instance-automated-backups')['DBInstanceAutomatedBackups']:
            # RDS does not expose ownership tags for retained automated backups.
            # Use the exact database names provisioned by this repository.
            if backup['DBInstanceIdentifier'] in FOUNDATIONS:
                result['backups'].append(backup['DBInstanceAutomatedBackupsArn'])
        secrets = self.aws.call('secretsmanager', 'list-secrets', '--include-planned-deletion',
                                '--filters', f'Key=name,Values={PROJECT}/')['SecretList']
        for secret in secrets:
            if not secret['Name'].startswith(PROJECT + '/'):
                continue
            if tags(secret.get('Tags', [])).get('Project') != PROJECT:
                raise ValueError(f"Cannot attribute secret {secret['Name']} safely.")
            result['secrets'].append(secret['ARN'])
        for prefix in (LOG_PREFIX, CLEANUP_LOG):
            for group in self.aws.call('logs', 'describe-log-groups', '--log-group-name-prefix', prefix)['logGroups']:
                name = group['logGroupName']
                if name.startswith(LOG_PREFIX) or name == CLEANUP_LOG:
                    result['logs'].append(name)
        schedules = self.aws.call('scheduler', 'list-schedules', '--group-name', 'default',
                                  '--name-prefix', PROJECT + '-')['Schedules']
        for schedule in schedules:
            name = schedule['Name']
            if name not in tuple(f'{foundation}-cleanup' for foundation in FOUNDATIONS):
                result['blockers'].append(schedule['Arn'])
            else:
                details = self.aws.call('scheduler', 'get-schedule', '--group-name', 'default', '--name', name)
                target = f'arn:aws:lambda:{self.aws.region}:{self.aws.account}:function:{PROJECT}-cleanup'
                if details['Target']['Arn'] != target:
                    raise ValueError(f'{name}: schedule targets another project.')
                result['schedules'].append(name)
        for instance in self.aws.call('rds', 'describe-db-instances')['DBInstances']:
            if instance['DBInstanceIdentifier'] in FOUNDATIONS or tags(instance.get('TagList', [])).get('Project') == PROJECT:
                result['blockers'].append(instance['DBInstanceArn'])
        clusters = self.aws.call('ecs', 'list-clusters')['clusterArns']
        result['blockers'].extend(arn for arn in clusters if arn.rsplit('/', 1)[-1].startswith(PROJECT + '-'))
        lbs = self.aws.call('elbv2', 'describe-load-balancers')['LoadBalancers']
        for offset in range(0, len(lbs), 20):
            values = self.aws.call('elbv2', 'describe-tags', '--resource-arns',
                                  *[item['LoadBalancerArn'] for item in lbs[offset:offset + 20]])['TagDescriptions']
            result['blockers'].extend(item['ResourceArn'] for item in values if tags(item['Tags']).get('Project') == PROJECT)
        # VPC deletion also proves that task/ALB network interfaces have gone.
        vpcs = self.aws.call('ec2', 'describe-vpcs', '--filters', f'Name=tag:Project,Values={PROJECT}')['Vpcs']
        result['blockers'].extend(item['VpcId'] for item in vpcs)
        addresses = self.aws.call('ec2', 'describe-addresses', '--filters', f'Name=tag:Project,Values={PROJECT}')['Addresses']
        result['blockers'].extend(item['AllocationId'] for item in addresses)
        function = self.optional(('lambda', 'get-function', '--function-name', f'{PROJECT}-cleanup'),
                                 'ResourceNotFoundException')
        if function:
            result['functions'].append(f'{PROJECT}-cleanup')
        # Catch tagged billable resources outside the normal templates, rather
        # than claiming success after deleting just the expected stack names.
        resources = self.aws.call('resourcegroupstaggingapi', 'get-resources',
                                  '--tag-filters', f'Key=Project,Values={PROJECT}')['ResourceTagMappingList']
        billable = re.compile(r'^arn:aws:(s3|dynamodb|backup|opensearch):|'
                              r'^arn:aws:ec2:[^:]+:[^:]+:(instance|volume|natgateway|elastic-ip)/')
        result['blockers'].extend(item['ResourceARN'] for item in resources if billable.search(item['ResourceARN']))
        self.remaining = result
        return result

    def wait_until(self, check, description, seconds=2700):
        deadline = time.monotonic() + seconds
        while not check():
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Timed out waiting for {description}; rerun cleanup after checking AWS.')
            time.sleep(15)

    def delete_stack(self, stack, role):
        name, identity = stack['StackName'], stack['StackId']
        status = stack['StackStatus']
        if status != 'DELETE_IN_PROGRESS':
            if status.endswith('_IN_PROGRESS'):
                raise ValueError(f'{name} is busy ({status}); retry when it has finished.')
            self.aws.call('cloudformation', 'delete-stack', '--stack-name', identity, '--role-arn', role)
        def complete():
            current = self.aws.describe(identity)
            if current and current['StackStatus'] == 'DELETE_FAILED':
                raise ValueError(f'{name}: deletion failed. Check CloudFormation events and retry.')
            return not current or current['StackStatus'] == 'DELETE_COMPLETE'
        self.wait_until(complete, name)
        self.deleted.append(name)
        print(f'Deleted {name}', flush=True)

    def stop_tasks(self, stack):
        outputs = {item['OutputKey']: item['OutputValue'] for item in stack.get('Outputs', [])}
        cluster = outputs.get('ClusterArn')
        if not cluster:
            return
        expected = f'arn:aws:ecs:{self.aws.region}:{self.aws.account}:cluster/{stack["StackName"]}'
        if cluster != expected:
            raise ValueError('Unexpected cluster; refusing to stop tasks.')
        tasks = self.optional(('ecs', 'list-tasks', '--cluster', cluster, '--desired-status', 'RUNNING'),
                              'ClusterNotFoundException')
        for task in (tasks or {}).get('taskArns', []):
            self.aws.call('ecs', 'stop-task', '--cluster', cluster, '--task', task,
                          '--reason', 'Permanent Delivery Lab retirement')
        for offset in range(0, len((tasks or {}).get('taskArns', [])), 100):
            self.aws.call('ecs', 'wait', 'tasks-stopped', '--cluster', cluster,
                          '--tasks', *tasks['taskArns'][offset:offset + 100])

    def remove_retained(self, inventory):
        for name in inventory['schedules']:
            self.aws.call('scheduler', 'delete-schedule', '--group-name', 'default', '--name', name)
        for name in inventory['functions']:
            self.aws.call('lambda', 'delete-function', '--function-name', name)
        for name in inventory['repositories']:
            self.aws.call('ecr', 'delete-repository', '--repository-name', name, '--force')
        for name in inventory['snapshots']:
            def ready():
                value = self.optional(('rds', 'describe-db-snapshots', '--db-snapshot-identifier', name),
                                      'DBSnapshotNotFound')
                return not value or value['DBSnapshots'][0]['Status'] in ('available', 'deleting')
            self.wait_until(ready, f'snapshot {name}')
            value = self.optional(('rds', 'describe-db-snapshots', '--db-snapshot-identifier', name),
                                  'DBSnapshotNotFound')
            if value and value['DBSnapshots'][0]['Status'] != 'deleting':
                self.aws.call('rds', 'delete-db-snapshot', '--db-snapshot-identifier', name)
        for arn in inventory['backups']:
            self.aws.call('rds', 'delete-db-instance-automated-backup', '--db-instance-automated-backups-arn', arn)
        for arn in inventory['secrets']:
            self.aws.call('secretsmanager', 'delete-secret', '--secret-id', arn, '--force-delete-without-recovery')
        for name in inventory['logs']:
            self.aws.call('logs', 'delete-log-group', '--log-group-name', name)
        for kind, values in inventory.items():
            if kind != 'blockers':
                self.deleted.extend(values)

    def retire(self):
        stacks = self.stacks()
        # Inspect ownership of retained data before making any destructive call.
        self.inventory()
        access = stacks.get(f'{PROJECT}-access')
        if access:
            template = self.aws.call('cloudformation', 'get-template', '--stack-name', access['StackId'])['TemplateBody']
            if not provider_is_retained(template):
                raise ValueError('Install the updated access template first; keep the shared GitHub identity provider.')
        elif any(name in stacks for name in STACKS[:-1]):
            raise ValueError('Access stack is absent while dependent stacks remain; restore access before cleanup.')
        for name in FOUNDATIONS:
            for suffix in ('-app', '-migration'):
                if name + suffix in stacks:
                    self.delete_stack(stacks[name + suffix], self.aws.role)
            if name in stacks:
                self.stop_tasks(stacks[name])
                self.delete_stack(stacks[name], self.aws.role)
        if f'{PROJECT}-shared' in stacks:
            self.delete_stack(stacks[f'{PROJECT}-shared'], self.aws.role)
        # Delete access last: this stops future CI publication and removes the
        # cleanup Lambda/log group without leaving drift in the access stack.
        if access:
            self.delete_stack(access, self.cleanup_role)
        remaining = self.inventory()
        self.remove_retained(remaining)
        result = {}
        def empty():
            result.clear()
            result.update(self.inventory())
            result['stacks'] = list(self.stacks())
            self.remaining = dict(result)
            return not any(result.values())
        self.wait_until(empty, 'absence of remaining project resources', seconds=600)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='eu-west-1', choices=['eu-west-1'])
    parser.add_argument('--profile')
    parser.add_argument('--confirm', required=True)
    parser.add_argument('--output', default='final-cleanup.json')
    args = parser.parse_args()
    report = {'status': 'failed', 'project': PROJECT, 'account': args.account, 'region': args.region,
              'scope': 'Resources provisioned by this repository; not unrelated account resources or other regions.'}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    cleanup = None
    try:
        if args.confirm != CONFIRMATION:
            raise ValueError(f'Permanent deletion requires --confirm "{CONFIRMATION}".')
        aws = Aws(args.account, args.region, args.profile)
        aws.verify()
        cleanup = FinalCleanup(aws)
        report['remaining'] = cleanup.retire()
        report['status'] = 'passed'
        report['retained_free_resources'] = ['delivery-lab-final-cleanup IAM stack/role', 'GitHub OIDC provider']
    except (ValueError, TimeoutError, subprocess.CalledProcessError) as error:
        report['error'] = getattr(error, 'stderr', None) or str(error)
        raise
    finally:
        report['completed_at'] = datetime.now(timezone.utc).isoformat()
        report['deleted'] = cleanup.deleted if cleanup else []
        if 'remaining' not in report:
            report['remaining'] = cleanup.remaining if cleanup else None
        output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, TimeoutError, subprocess.CalledProcessError) as error:
        print(getattr(error, 'stderr', None) or str(error), file=sys.stderr)
        sys.exit(1)
