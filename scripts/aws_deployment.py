#!/usr/bin/env python3
"""Shared AWS deployment engine for existing, unexpired lab foundations."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from lab import extract_release, now, read_json, sha256, verify_release, write_json
from smoke import smoke

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra/aws'))
from provision import Aws, PROJECT

DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
ENVIRONMENTS = ('dev', 'uat', 'prod')
READY = {'CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'}


def outputs(stack):
    return {item['OutputKey']: item['OutputValue'] for item in stack.get('Outputs', [])}


def validate_release(archive, metadata, directory, account, region, commit, repository, ci_run):
    if not re.fullmatch(r'[0-9a-f]{40}', commit) or not re.fullmatch(r'[1-9][0-9]*', ci_run):
        raise ValueError('Use the full commit SHA and numeric source CI run ID.')
    data = read_json(metadata)
    expected = {'schema_version': 1, 'release': commit, 'platform': 'linux/amd64',
                'aws_account': account, 'aws_region': region}
    if any(data.get(key) != value for key, value in expected.items()):
        raise ValueError('AWS release metadata has the wrong source, account, region or platform.')
    source = data['source']
    if source['repository'] != repository or source['ci_run_id'] != ci_run:
        raise ValueError('AWS metadata is from another repository or CI run.')
    artifact = data['artifact']
    if (artifact['name'] != f'release-{commit}' or artifact['file'] != 'release.tar.gz'
            or artifact['archive_sha256'] != sha256(archive)):
        raise ValueError('Release archive does not match its AWS metadata.')
    extract_release(archive, directory)
    manifest = verify_release(directory)
    if manifest['release'] != commit or sha256(directory / 'manifest.json') != artifact['manifest_sha256']:
        raise ValueError('Release manifest does not match its AWS metadata.')
    if set(data['images']) != {'backend', 'frontend'}:
        raise ValueError('The release must identify both application images.')
    for service, image in data['images'].items():
        repo = f'{account}.dkr.ecr.{region}.amazonaws.com/{PROJECT}/{service}'
        if (image['repository'] != repo or image['tag'] != commit or not DIGEST.fullmatch(image['digest'])
                or image['uri'] != f'{repo}@{image["digest"]}'):
            raise ValueError('Image references must use the lab repositories and immutable digests.')
    return data


def check_foundation(stack, account, minimum_minutes=60, environment='dev'):
    if environment not in ENVIRONMENTS:
        raise ValueError('Unknown deployment environment.')
    foundation = f'{PROJECT}-{environment}'
    label = environment.upper()
    if not stack or stack['StackStatus'] not in READY:
        raise ValueError(f'{label} foundation is absent or not ready. Provision it explicitly first.')
    if not stack['StackId'].startswith(f'arn:aws:cloudformation:eu-west-1:{account}:stack/{foundation}/'):
        raise ValueError(f'Unexpected {label} foundation identity.')
    tags = {tag['Key']: tag['Value'] for tag in stack.get('Tags', [])}
    if tags.get('Project') != PROJECT or tags.get('Purpose') != 'academic-lab':
        raise ValueError(f'{label} foundation ownership tags are missing.')
    values = outputs(stack)
    deadline = datetime.fromisoformat(values['ExpiresAt']).replace(tzinfo=timezone.utc)
    if deadline <= datetime.now(timezone.utc) + timedelta(minutes=minimum_minutes):
        raise ValueError(f'{label} has insufficient time before cleanup; start a new session explicitly.')
    if values['EnvironmentName'] != environment:
        raise ValueError('Foundation targets another environment.')
    parameters = {p['ParameterKey']: p['ParameterValue'] for p in stack['Parameters']}
    if parameters.get('CertificateArn'):
        raise ValueError('This HTTP lab needs a configured hostname before HTTPS smoke testing.')
    return values


def resolve_images(aws, data):
    """Verify stored tags/digests; resolve a single x86 image if ECR holds an OCI index."""
    runtime = {}
    for service, image in data['images'].items():
        result = aws.call('ecr', 'batch-get-image', '--registry-id', aws.account,
                          '--repository-name', f'{PROJECT}/{service}',
                          '--image-ids', f'imageTag={data["release"]}')
        images = result.get('images', [])
        if result.get('failures') or len(images) != 1 or images[0]['imageId']['imageDigest'] != image['digest']:
            raise ValueError(f'{service}: ECR does not match the published release digest.')
        manifest = json.loads(images[0]['imageManifest'])
        digest = image['digest']
        if 'manifests' in manifest:
            candidates = [m['digest'] for m in manifest['manifests']
                          if m.get('platform', {}).get('os') == 'linux'
                          and m.get('platform', {}).get('architecture') == 'amd64']
            if len(candidates) != 1 or not DIGEST.fullmatch(candidates[0]):
                raise ValueError('The OCI index must identify one Linux/amd64 application image.')
            digest = candidates[0]
        runtime[service] = digest
    return runtime


class Deployment:
    def __init__(self, aws, foundation, directory, data, environment='dev'):
        self.environment = environment
        self.name = f'{PROJECT}-{environment}'
        self.aws, self.foundation, self.directory, self.data = aws, foundation, directory, data
        self.values = check_foundation(foundation, aws.account, environment=environment)
        self.cluster = self.values['ClusterArn']

    def still_active(self):
        current = self.aws.describe(self.name)
        check_foundation(current, self.aws.account, minimum_minutes=15, environment=self.environment)
        if current['StackId'] != self.foundation['StackId']:
            raise ValueError('The environment was recreated while this deployment was running.')

    def stack(self, suffix, template, parameters):
        self.still_active()
        name = self.name + suffix
        current = self.aws.describe(name)
        if current:
            tags = {tag['Key']: tag['Value'] for tag in current.get('Tags', [])}
            if tags.get('ParentStackId') != self.foundation['StackId'] or tags.get('Project') != PROJECT:
                raise ValueError('Refusing to update a release stack belonging to another environment session.')
            if current['StackStatus'] not in READY:
                raise ValueError(f'{name} is {current["StackStatus"]}; resolve its CloudFormation failure before retrying.')
        print(f'Applying {name} for {self.data["release"]}...', flush=True)
        subprocess.run(self.aws.command + [
            'cloudformation', 'deploy', '--stack-name', name,
            '--template-file', str(self.directory / 'infra/aws' / template),
            '--role-arn', self.aws.role, '--capabilities', 'CAPABILITY_NAMED_IAM',
            '--no-fail-on-empty-changeset', '--tags', f'Project={PROJECT}', 'Purpose=academic-lab',
            f'ParentStackId={self.foundation["StackId"]}', '--parameter-overrides',
            *[f'{key}={value}' for key, value in parameters.items()]], check=True, timeout=1800)
        result = self.aws.describe(name)
        if result['StackStatus'] not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
            raise ValueError(f'{name} did not successfully apply the requested release.')
        return outputs(result)

    def run_task(self, definition, container):
        self.still_active()
        expected = f'arn:aws:ecs:eu-west-1:{self.aws.account}:task-definition/{self.name}-migration-{container}:'
        if not definition.startswith(expected):
            raise ValueError('Unexpected database task definition.')
        network = {'awsvpcConfiguration': {'subnets': self.values['TaskSubnetIds'].split(','),
                   'securityGroups': [self.values['TaskSecurityGroupId']], 'assignPublicIp': 'ENABLED'}}
        result = self.aws.call('ecs', 'run-task', '--cluster', self.cluster, '--task-definition', definition,
                               '--launch-type', 'FARGATE', '--platform-version', '1.4.0', '--count', '1',
                               '--client-token', uuid.uuid4().hex, '--started-by', self.name,
                               '--network-configuration', json.dumps(network))
        tasks = result.get('tasks', [])
        if result.get('failures') or len(tasks) != 1:
            raise ValueError(f'{container}: ECS did not start exactly one task: {result.get("failures", [])}')
        task_arn = tasks[0]['taskArn']
        print(f'Waiting for {container}: {task_arn}', flush=True)
        stopped = False
        try:
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                result = self.aws.call('ecs', 'describe-tasks', '--cluster', self.cluster, '--tasks', task_arn)
                if result.get('failures') or len(result.get('tasks', [])) != 1:
                    raise ValueError(f'{container}: ECS task status is unavailable.')
                task = result['tasks'][0]
                if task['lastStatus'] == 'STOPPED':
                    stopped = True
                    containers = task.get('containers', [])
                    if (task.get('stopCode') != 'EssentialContainerExited' or len(containers) != 1
                            or containers[0].get('name') != container or containers[0].get('exitCode') != 0):
                        raise ValueError(f'{container} failed: {task.get("stoppedReason", "unknown reason")}; '
                                         f'exit codes {[c.get("exitCode") for c in containers]}. Inspect CloudWatch logs.')
                    print(f'{container} exited successfully.', flush=True)
                    return task_arn
                time.sleep(10)
            raise TimeoutError(f'{container} exceeded its 15-minute deadline.')
        finally:
            if not stopped:
                self.aws.call('ecs', 'stop-task', '--cluster', self.cluster, '--task', task_arn,
                              '--reason', 'Delivery Lab database task did not finish within the deployment.')

    def verify_service(self, service, definition, runtime):
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            result = self.aws.call('ecs', 'describe-services', '--cluster', self.cluster, '--services', service)
            if result.get('failures') or len(result.get('services', [])) != 1:
                raise ValueError('ECS application service is unavailable.')
            state = result['services'][0]
            deployments = state.get('deployments', [])
            if any(d.get('rolloutState') == 'FAILED' for d in deployments):
                raise ValueError('ECS reported a failed deployment.')
            if (len(deployments) == 1 and deployments[0].get('rolloutState') == 'COMPLETED'
                    and state['runningCount'] == state['desiredCount'] == 1 and state['pendingCount'] == 0):
                if state['taskDefinition'] != definition:
                    raise ValueError('ECS stabilized on another revision, possibly after rollback.')
                break
            time.sleep(10)
        else:
            raise TimeoutError('ECS did not stabilize on the requested release within 15 minutes.')
        arns = self.aws.call('ecs', 'list-tasks', '--cluster', self.cluster, '--service-name',
                             service.rsplit('/', 1)[-1], '--desired-status', 'RUNNING')['taskArns']
        if len(arns) != 1:
            raise ValueError('Expected exactly one running application task.')
        result = self.aws.call('ecs', 'describe-tasks', '--cluster', self.cluster, '--tasks', *arns)
        if result.get('failures') or len(result.get('tasks', [])) != 1:
            raise ValueError('Running task details are unavailable.')
        task = result['tasks'][0]
        containers = {c['name']: c for c in task['containers']}
        if (task['taskDefinitionArn'] != definition or task.get('lastStatus') != 'RUNNING'
                or task.get('healthStatus') != 'HEALTHY'
                or set(containers) != set(runtime)
                or any(containers[name].get('imageDigest') != digest for name, digest in runtime.items())):
            raise ValueError('Running ECS task health or image digests do not match the release.')
        return arns

    def deploy(self, runtime):
        parameters = {'EnvironmentStackName': self.name, 'SharedStackName': PROJECT + '-shared',
                      'ReleaseId': self.data['release'], 'BackendImageDigest': self.data['images']['backend']['digest']}
        migration = self.stack('-migration', 'migration.yaml', parameters)
        bootstrap_task = self.run_task(migration['BootstrapTaskDefinitionArn'], 'bootstrap')
        migration_task = self.run_task(migration['MigrationTaskDefinitionArn'], 'migrate')
        parameters.update(FrontendImageDigest=self.data['images']['frontend']['digest'], DesiredCount='1')
        app = self.stack('-app', 'application.yaml', parameters)
        tasks = self.verify_service(app['ServiceArn'], app['ApplicationTaskDefinitionArn'], runtime)
        self.still_active()
        url = 'http://' + self.values['LoadBalancerDnsName']
        smoke(url, self.data['release'], self.environment)
        return {'status': 'passed', 'environment': self.environment, 'release': self.data['release'],
                'source': self.data['source'], 'artifact': self.data['artifact'], 'images': self.data['images'],
                'foundation_stack_id': self.foundation['StackId'], 'expires_at': self.values['ExpiresAt'],
                'service_arn': app['ServiceArn'], 'task_definition_arn': app['ApplicationTaskDefinitionArn'],
                'bootstrap_task_arn': bootstrap_task, 'migration_task_arn': migration_task,
                'application_task_arns': tasks, 'url': url, 'completed_at': now()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--metadata', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--ci-run-id', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='eu-west-1', choices=['eu-west-1'])
    parser.add_argument('--profile')
    parser.add_argument('--require-role', choices=['delivery-lab-github-dev'],
                        help='In GitHub, reject ambient personal credentials on the self-hosted runner.')
    args = parser.parse_args()
    receipt = {'status': 'failed', 'environment': 'dev', 'release': args.release}
    # Invalidate a previous receipt before any operation can fail.
    write_json(args.output, receipt)
    try:
        with tempfile.TemporaryDirectory(prefix='delivery-lab-dev-') as temporary:
            directory = Path(temporary)
            data = validate_release(args.archive, args.metadata, directory, args.account, args.region,
                                    args.release, args.repository, args.ci_run_id)
            aws = Aws(args.account, args.region, args.profile)
            identity = aws.verify()
            if args.require_role and not identity['Arn'].startswith(
                    f'arn:aws:sts::{args.account}:assumed-role/{args.require_role}/'):
                raise ValueError('Deployment requires the configured temporary GitHub DEV role.')
            foundation = aws.describe(PROJECT + '-dev')
            values = check_foundation(foundation, aws.account)
            runtime = resolve_images(aws, data)
            receipt = Deployment(aws, foundation, directory, data).deploy(runtime)
    finally:
        write_json(args.output, receipt)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(getattr(error, 'stderr', None) or str(error), file=sys.stderr)
        sys.exit(1)
