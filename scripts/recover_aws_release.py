#!/usr/bin/env python3
"""Restore verified application images, retaining infrastructure and database state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
import zipfile

from aws_deployment import (Aws, Deployment, ENVIRONMENTS, PROJECT, READY, check_foundation,
                            check_runner_network, outputs, resolve_images, validate_release)
from lab import now, write_json
from promote_aws_release import GitHub, current_execution, same_release, timestamp
from smoke import smoke

ROOT = Path(__file__).resolve().parents[1]


def parameters(stack):
    return {item['ParameterKey']: item['ParameterValue'] for item in stack['Parameters']}


def migration_bundle(jar):
    """A conservative release-level check, not a query of the live Oracle schema."""
    checksums = {}
    with zipfile.ZipFile(jar) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate entries in the backend JAR.')
        scripts = [name for name in names if name.startswith('BOOT-INF/classes/db/migration/') and not name.endswith('/')]
        if not scripts or any(not re.fullmatch(r'BOOT-INF/classes/db/migration/V[0-9]+(?:_[0-9]+)*__[A-Za-z0-9_]+\.sql', name) for name in scripts):
            raise ValueError('Recovery requires versioned SQL migrations in the packaged backend.')
        libraries = [name for name in names if re.fullmatch(r'BOOT-INF/lib/flyway-.*\.jar', name)]
        config = [name for name in names if re.fullmatch(r'BOOT-INF/classes/application(?:-[\w-]+)?\.(?:yml|yaml|properties)', name)]
        if not libraries or not config:
            raise ValueError('The migration engine/configuration cannot be verified.')
        for name in sorted(scripts + libraries + config):
            checksums[name] = hashlib.sha256(archive.read(name)).hexdigest()
    return checksums


def require_same_migrations(target, latest):
    expected, actual = migration_bundle(target / 'backend.jar'), migration_bundle(latest / 'backend.jar')
    if expected != actual:
        raise ValueError('Database compatibility is not established: migrations, Flyway or configuration changed. '
                         'Use a tested forward fix; recovery never reverses SQL or repairs migration history.')
    return hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()


def load_release(github, ci_run, release, directory, account, region):
    run = github.run(str(ci_run), 'ci', release)
    archive, metadata = directory / 'release.tar.gz', directory / 'aws-release.json'
    github.artifact(run, f'release-{release}', 'release.tar.gz', archive)
    github.artifact(run, f'aws-release-{release}', 'aws-release.json', metadata)
    unpacked = directory / 'unpacked'
    data = validate_release(archive, metadata, unpacked, account, region, release, github.repository, str(ci_run))
    return data, unpacked


def find_ci(github, release):
    if not re.fullmatch(r'[0-9a-f]{40}', release):
        raise ValueError('Migration stack has an invalid release SHA.')
    runs = github.api(f'{github.base}/workflows/ci.yml/runs?head_sha={release}&event=push&branch=main&status=success&per_page=100')
    if runs['total_count'] != 1 or len(runs['workflow_runs']) != 1:
        raise ValueError('Cannot identify one successful CI run for the latest migration release.')
    return str(runs['workflow_runs'][0]['id'])


def owned_child(stack, foundation, name):
    if not stack or stack['StackStatus'] not in READY:
        raise ValueError(f'{name} is absent, busy or failed; resolve its stack state before recovery.')
    tags = {tag['Key']: tag['Value'] for tag in stack.get('Tags', [])}
    if (tags.get('Project') != PROJECT or tags.get('ParentStackId') != foundation['StackId']
            or parameters(stack).get('EnvironmentStackName') != name.rsplit('-', 1)[0]):
        raise ValueError('Recovery refuses a child stack from another environment/session.')


def migration_task_evidence(aws, environment, migration):
    """Require recent successful database tasks; do not guess if ECS history expired."""
    name = f'{PROJECT}-{environment}'
    cluster = f'arn:aws:ecs:{aws.region}:{aws.account}:cluster/{name}'
    evidence = {}
    for container, output in [('bootstrap', 'BootstrapTaskDefinitionArn'), ('migrate', 'MigrationTaskDefinitionArn')]:
        definition = outputs(migration)[output]
        family = f'{name}-migration-{container}'
        if not definition.startswith(f'arn:aws:ecs:{aws.region}:{aws.account}:task-definition/{family}:'):
            raise ValueError('Unexpected database task family.')
        running = aws.call('ecs', 'list-tasks', '--cluster', cluster, '--family', family, '--desired-status', 'RUNNING')['taskArns']
        if running:
            raise ValueError('A database task is still running; wait before recovery.')
        stopped = aws.call('ecs', 'list-tasks', '--cluster', cluster, '--family', family, '--desired-status', 'STOPPED')['taskArns']
        tasks = []
        for offset in range(0, len(stopped), 100):
            response = aws.call('ecs', 'describe-tasks', '--cluster', cluster, '--tasks', *stopped[offset:offset + 100])
            if response.get('failures'):
                raise ValueError('Database task history is incomplete.')
            tasks.extend(response.get('tasks', []))
        if not tasks:
            raise ValueError('Recent database task evidence is unavailable. Investigate/forward-fix; do not bypass this gate.')
        latest = max(tasks, key=lambda task: timestamp(task['createdAt']))
        containers = latest.get('containers', [])
        overrides = latest.get('overrides', {}).get('containerOverrides', [])
        expected_image = (f'{aws.account}.dkr.ecr.{aws.region}.amazonaws.com/{PROJECT}/backend@'
                          + outputs(migration)['BackendImageDigest'])
        if (latest['taskDefinitionArn'] != definition or latest['lastStatus'] != 'STOPPED'
                or latest.get('stopCode') != 'EssentialContainerExited' or len(containers) != 1
                or containers[0].get('name') != container or containers[0].get('exitCode') != 0
                or containers[0].get('image') != expected_image
                or any(set(override) - {'name'} for override in overrides)):
            raise ValueError('The latest database task did not succeed; inspect Oracle/migration failure before recovery.')
        evidence[container] = latest['taskArn']
    return evidence


def check_application_template(aws, app):
    # Keep the current, reviewed ECS-only template. A different template requires review,
    # rather than guessing whether its backend startup could run SQL or custom commands.
    body = aws.call('cloudformation', 'get-template', '--stack-name', app['StackId'])['TemplateBody']
    expected = (ROOT / 'infra/aws/application.yaml').read_text()
    if not isinstance(body, str) or body.strip() != expected.strip():
        raise ValueError('Application template differs from the reviewed recovery controller; review/forward-fix first.')


def snapshot(stack):
    return {key: stack.get(key) for key in ('StackId', 'StackStatus', 'LastUpdatedTime', 'Parameters')}


class Recovery:
    def __init__(self, aws, environment, foundation, app, migration, data, directory):
        self.aws, self.environment, self.foundation = aws, environment, foundation
        self.app, self.migration, self.data = app, migration, data
        self.name = f'{PROJECT}-{environment}'
        self.engine = Deployment(aws, foundation, directory, data, environment)

    def unchanged(self):
        self.engine.still_active()
        for suffix, original in [('-app', self.app), ('-migration', self.migration)]:
            if snapshot(self.aws.describe(self.name + suffix) or {}) != snapshot(original):
                raise ValueError('Environment changed during recovery planning; stop and review a new plan.')

    def apply(self):
        self.unchanged()
        values = parameters(self.app)
        changes = {'ReleaseId': self.data['release'],
                   'FrontendImageDigest': self.data['images']['frontend']['digest'],
                   'BackendImageDigest': self.data['images']['backend']['digest']}
        if all(values.get(key) == value for key, value in changes.items()):
            # The circuit breaker/CloudFormation may already have restored this release.
            return {'change_set': None, 'already_configured': True}
        update = [{'ParameterKey': key, 'ParameterValue': changes[key]} if key in changes
                  else {'ParameterKey': key, 'UsePreviousValue': True} for key in values]
        change_set = self.aws.call('cloudformation', 'create-change-set', '--stack-name', self.app['StackId'],
                    '--change-set-name', 'recovery-' + uuid.uuid4().hex, '--change-set-type', 'UPDATE',
                    '--use-previous-template', '--role-arn', self.aws.role,
                    '--parameters', json.dumps(update), '--capabilities', 'CAPABILITY_NAMED_IAM')['Id']
        executed = False
        try:
            self.aws.call('cloudformation', 'wait', 'change-set-create-complete', '--change-set-name', change_set)
            plan = self.aws.call('cloudformation', 'describe-change-set', '--change-set-name', change_set)
            allowed = {'ApplicationTask': 'AWS::ECS::TaskDefinition', 'ApplicationService': 'AWS::ECS::Service'}
            resources = [change['ResourceChange'] for change in plan.get('Changes', [])]
            if (plan['Status'] != 'CREATE_COMPLETE' or not resources
                    or any(r['Action'] != 'Modify' or allowed.get(r['LogicalResourceId']) != r['ResourceType']
                           or (r['LogicalResourceId'] == 'ApplicationService' and r.get('Replacement') != 'False')
                           for r in resources)):
                raise ValueError('Recovery change set must only update the application task and existing ECS service.')
            self.unchanged()
            migration_task_evidence(self.aws, self.environment, self.migration)
            self.aws.call('cloudformation', 'execute-change-set', '--change-set-name', change_set)
            executed = True
            subprocess.run(self.aws.command + ['cloudformation', 'wait', 'stack-update-complete',
                                               '--stack-name', self.app['StackId']], check=True, timeout=1800)
            return {'change_set': change_set, 'already_configured': False}
        finally:
            if not executed:
                self.aws.call('cloudformation', 'delete-change-set', '--change-set-name', change_set)

    def verify(self, runtime):
        self.engine.still_active()
        app = self.aws.describe(self.name + '-app')
        owned_child(app, self.foundation, self.name + '-app')
        values = outputs(app)
        if (values['ReleaseId'] != self.data['release']
                or any(values[f'{service.title()}ImageDigest'] != self.data['images'][service]['digest']
                       for service in ('frontend', 'backend'))):
            raise ValueError('Application stack did not restore the requested release.')
        tasks = self.engine.verify_service(values['ServiceArn'], values['ApplicationTaskDefinitionArn'], runtime)
        url = 'http://' + self.engine.values['LoadBalancerDnsName']
        smoke(url, self.data['release'], self.environment)
        return {'service_arn': values['ServiceArn'], 'task_definition_arn': values['ApplicationTaskDefinitionArn'],
                'application_task_arns': tasks, 'url': url}


def recover(args):
    if (args.environment not in ENVIRONMENTS or not re.fullmatch(r'[0-9a-f]{40}', args.release)
            or not re.fullmatch(r'[1-9][0-9]*', args.previous_run_id)):
        raise ValueError('Select an environment, full release SHA and successful deployment run ID.')
    if not args.verify_only and not args.confirm_recovery:
        raise ValueError('Applying recovery requires an explicit confirmation.')
    github = GitHub(args.repository)
    execution = current_execution(github, 'recover.yml')
    aws = Aws(args.account, args.region, args.profile)
    identity = aws.verify()
    role = f'{PROJECT}-github-{args.environment}'
    if args.require_role and not identity['Arn'].startswith(f'arn:aws:sts::{args.account}:assumed-role/{role}/'):
        raise ValueError('Recovery requires the temporary GitHub role for this environment.')
    name = f'{PROJECT}-{args.environment}'
    foundation = aws.describe(name)
    values = check_foundation(foundation, aws.account, environment=args.environment)
    check_runner_network(values['AllowedClientCidr'])
    app, migration = aws.describe(name + '-app'), aws.describe(name + '-migration')
    owned_child(app, foundation, name + '-app')
    owned_child(migration, foundation, name + '-migration')
    check_application_template(aws, app)
    if parameters(app).get('DesiredCount') != '1':
        raise ValueError('Recovery preserves the running one-task lab configuration; it does not start a stopped environment.')
    with tempfile.TemporaryDirectory(prefix='delivery-lab-recovery-') as temporary:
        directory = Path(temporary)
        previous, reference = github.evidence(args.environment, args.previous_run_id, args.release, directory)
        if previous['foundation_stack_id'] != foundation['StackId']:
            raise ValueError('The target was verified in another environment session; recovery is blocked.')
        target, target_dir = load_release(github, previous['source']['ci_run_id'], args.release,
                                         directory / 'target', aws.account, aws.region)
        same_release(previous, target)
        latest_sha = outputs(migration)['ReleaseId']
        latest_run = find_ci(github, latest_sha)
        latest, latest_dir = load_release(github, latest_run, latest_sha, directory / 'latest', aws.account, aws.region)
        if outputs(migration)['BackendImageDigest'] != latest['images']['backend']['digest']:
            raise ValueError('Migration stack does not match its verified CI image metadata.')
        fingerprint = require_same_migrations(target_dir, latest_dir)
        database_tasks = migration_task_evidence(aws, args.environment, migration)
        runtime = resolve_images(aws, target)
        recovery = Recovery(aws, args.environment, foundation, app, migration, target, target_dir)
        recovery.unchanged()
        receipt = {'status': 'verified-only', 'environment': args.environment, 'release': args.release,
                   'from_release': parameters(app)['ReleaseId'], 'execution': execution,
                   'target_evidence': reference, 'foundation_stack_id': foundation['StackId'],
                   'expires_at': values['ExpiresAt'], 'source': target['source'], 'images': target['images'],
                   'artifact': target['artifact'], 'migration_release': latest_sha,
                   'migration_bundle_sha256': fingerprint, 'database_tasks': database_tasks,
                   'requested_by': os.environ.get('GITHUB_TRIGGERING_ACTOR') or identity['Arn']}
        print(f"Recovery plan: {args.environment.upper()} {receipt['from_release']} -> {args.release}; migration bundle unchanged.", flush=True)
        if args.verify_only:
            return receipt
        receipt['update'] = recovery.apply()
        receipt.update(recovery.verify(runtime))
        # Recovery is separate evidence: it cannot replace an ordinary promotion pass.
        receipt.update(status='recovered', completed_at=now())
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=ENVIRONMENTS, required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--previous-run-id', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', choices=['eu-west-1'], default='eu-west-1')
    parser.add_argument('--profile')
    parser.add_argument('--require-role', action='store_true')
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--confirm-recovery', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    receipt = {'status': 'failed', 'environment': args.environment, 'release': args.release}
    write_json(args.output, receipt)
    try:
        receipt = recover(args)
    except Exception as error:
        receipt['error'] = str(error)[:1000]
        raise
    finally:
        write_json(args.output, receipt)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        print(getattr(error, 'stderr', None) or str(error), file=sys.stderr)
        sys.exit(1)
