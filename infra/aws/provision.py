#!/usr/bin/env python3
"""Provision the disposable AWS foundations using CloudFormation and AWS CLI v2.

Uses the caller's existing CLI profile or GitHub OIDC session. Never handles
database passwords or creates application tasks. An eight-hour expiry is required.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PROJECT = 'delivery-lab'
ENVIRONMENTS = ('dev', 'uat', 'prod')


def client_cidr(value):
    network = ipaddress.ip_network(value, strict=True)
    if network.version != 4 or network.prefixlen != 32 or not network.is_global:
        raise ValueError('Use your public IPv4 address with /32 for this lab.')
    return str(network)


def expiry():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%Y-%m-%dT%H:%M:%S')


class Aws:
    def __init__(self, account, region='eu-west-1', profile=None):
        if not account.isdigit() or len(account) != 12:
            raise ValueError('An explicit 12-digit AWS account is required.')
        self.account = account
        self.region = region
        self.command = ['aws', '--region', region, '--no-cli-pager']
        if profile:
            self.command += ['--profile', profile]
        self.role = f'arn:aws:iam::{account}:role/{PROJECT}-cloudformation'

    def call(self, *args):
        result = subprocess.run(self.command + list(args) + ['--output', 'json'],
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def verify(self):
        identity = self.call('sts', 'get-caller-identity')
        if identity['Account'] != self.account:
            raise ValueError('Authenticated AWS account does not match --account; refusing changes.')
        print(f"Verified {identity['Arn']} in {self.region}", flush=True)
        return identity

    def describe(self, name):
        try:
            return self.call('cloudformation', 'describe-stacks', '--stack-name', name)['Stacks'][0]
        except subprocess.CalledProcessError as error:
            if 'ValidationError' in error.stderr and 'does not exist' in error.stderr:
                return None
            raise

    def deploy(self, name, template, parameters):
        print(f'Provisioning {name} through CloudFormation...', flush=True)
        subprocess.run(self.command + [
            'cloudformation', 'deploy', '--stack-name', name,
            '--template-file', str(ROOT / template),
            '--role-arn', self.role, '--capabilities', 'CAPABILITY_NAMED_IAM',
            '--no-fail-on-empty-changeset', '--tags', f'Project={PROJECT}',
            'Purpose=academic-lab', '--parameter-overrides',
            *[f'{key}={value}' for key, value in parameters.items()]], check=True)

    def oracle_version(self):
        versions = self.call('rds', 'describe-db-engine-versions', '--engine',
                             'oracle-se2', '--default-only')['DBEngineVersions']
        version = versions[0]['EngineVersion']
        if not version.startswith('19.'):
            raise ValueError('The default Oracle version is no longer 19c; review compatibility first.')
        options = self.call('rds', 'describe-orderable-db-instance-options',
                            '--engine', 'oracle-se2', '--engine-version', version,
                            '--db-instance-class', 'db.t3.small',
                            '--license-model', 'license-included')['OrderableDBInstanceOptions']
        if not any(option['StorageType'] == 'gp3' and option['MinStorageSize'] <= 20
                   <= option['MaxStorageSize'] and option.get('Vpc') for option in options):
            raise ValueError('Oracle db.t3.small, 20 GiB gp3, License Included is unavailable.')
        return version

    def environment(self, env, cidr, version, initial_expiry):
        name = f'{PROJECT}-{env}'
        profile = json.loads((ROOT / 'parameters' / f'{env}.json').read_text())
        parameters = {item['ParameterKey']: item['ParameterValue'] for item in profile}
        # These are deliberate budget limits for this short-lived academic lab.
        expected = {'DatabaseInstanceClass': 'db.t3.small', 'DatabaseAllocatedStorage': '20',
                    'DatabaseMultiAZ': 'false', 'ProtectDatabase': 'false'}
        if any(parameters.get(key) != value for key, value in expected.items()):
            raise ValueError(f'{env}: profile exceeds the approved disposable lab configuration.')
        parameters.update(AllowedClientCidr=cidr, OracleEngineVersion=version,
                          ExpiresAt=initial_expiry)
        self.deploy(name, 'environment.yaml', parameters)
        return name

    def provision(self, cidr, environments=ENVIRONMENTS):
        if not environments or any(env not in ENVIRONMENTS for env in environments):
            raise ValueError('Select dev, uat, prod or all environments.')
        cidr = client_cidr(cidr)
        version = self.oracle_version()
        print(f'Oracle {version}; {len(environments)} db.t3.small instance(s), 20 GiB each, Single-AZ.', flush=True)
        self.deploy(f'{PROJECT}-shared', 'shared.yaml', {'ProjectName': PROJECT})
        # Schedule creation is part of each foundation, even if this process stops.
        initial_expiry = expiry()
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(self.environment, env, cidr, version, initial_expiry)
                       for env in environments]
            for future in futures:
                future.result()
        # Give the user a full eight hours after the selected foundations are ready.
        # Updates preserve all other parameters and resources.
        final_expiry = expiry()
        for env in environments:
            stack = self.describe(f'{PROJECT}-{env}')
            parameters = {item['ParameterKey']: item['ParameterValue'] for item in stack['Parameters']}
            parameters['ExpiresAt'] = final_expiry
            self.deploy(f'{PROJECT}-{env}', 'environment.yaml', parameters)
        self.status()

    def status(self):
        for suffix in ('shared', *ENVIRONMENTS):
            name = f'{PROJECT}-{suffix}'
            stack = self.describe(name)
            if not stack:
                print(f'{name}: absent', flush=True)
                continue
            outputs = {item['OutputKey']: item['OutputValue'] for item in stack.get('Outputs', [])}
            print(json.dumps({'stack': name, 'status': stack['StackStatus'],
                              'expires_at_utc': outputs.get('ExpiresAt'),
                              'outputs': outputs}, indent=2), flush=True)

    def delete(self):
        # Remove owned release stacks first so their imports do not block deletion.
        for env in ENVIRONMENTS:
            name = f'{PROJECT}-{env}'
            stack = self.describe(name)
            if not stack:
                continue
            tags = {tag['Key']: tag['Value'] for tag in stack.get('Tags', [])}
            if tags.get('Project') != PROJECT or tags.get('Purpose') != 'academic-lab':
                raise ValueError(f'Refusing to delete {name}: expected ownership tags are missing.')
            children = [child for suffix in ('-app', '-migration') if (child := self.describe(name + suffix))]
            for child in children:
                child_tags = {tag['Key']: tag['Value'] for tag in child.get('Tags', [])}
                if child_tags.get('ParentStackId') != stack['StackId']:
                    raise ValueError('Refusing to delete a child without matching ParentStackId.')
            for child in children:
                self.call('cloudformation', 'delete-stack', '--stack-name', child['StackId'], '--role-arn', self.role)
                self.call('cloudformation', 'wait', 'stack-delete-complete', '--stack-name', child['StackId'])
            cluster = f'arn:aws:ecs:{self.region}:{self.account}:cluster/{name}'
            if any(o['OutputKey'] == 'ClusterArn' for o in stack.get('Outputs', [])):
                tasks = self.call('ecs', 'list-tasks', '--cluster', cluster, '--desired-status', 'RUNNING')['taskArns']
                for task in tasks:
                    self.call('ecs', 'stop-task', '--cluster', cluster, '--task', task,
                              '--reason', 'Explicit Delivery Lab environment deletion')
                for index in range(0, len(tasks), 100):
                    self.call('ecs', 'wait', 'tasks-stopped', '--cluster', cluster, '--tasks', *tasks[index:index + 100])
            self.call('cloudformation', 'delete-stack', '--stack-name', stack['StackId'],
                      '--role-arn', self.role)
            print(f'{name}: deletion requested (asynchronous)', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['verify', 'provision', 'status', 'delete'])
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='eu-west-1', choices=['eu-west-1'])
    parser.add_argument('--profile')
    parser.add_argument('--client-cidr', help='Your public IPv4/32; required for provision.')
    parser.add_argument('--environment', choices=['all', *ENVIRONMENTS], default='all',
                        help='Foundations to create/refresh during provision; default: all.')
    args = parser.parse_args()
    aws = Aws(args.account, args.region, args.profile)
    aws.verify()
    if args.action == 'provision':
        if not args.client_cidr:
            parser.error('--client-cidr is required for provision')
        aws.provision(args.client_cidr, ENVIRONMENTS if args.environment == 'all' else (args.environment,))
    elif args.action == 'status':
        aws.status()
    elif args.action == 'delete':
        aws.delete()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        print(getattr(error, 'stderr', None) or str(error), file=sys.stderr)
        sys.exit(1)
