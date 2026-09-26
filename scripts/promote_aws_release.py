#!/usr/bin/env python3
"""Promote a GitHub-verified release to AWS UAT/PROD without rebuilding images."""
import argparse
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

from aws_deployment import (Aws, Deployment, PROJECT, check_foundation,
                            check_runner_network, outputs, resolve_images, validate_release)
from lab import read_json, write_json

WORKFLOWS = {'dev': ('deploy-dev.yml', 'workflow_run'),
             'uat': ('promote.yml', 'workflow_dispatch'), 'ci': ('ci.yml', 'push')}


def check_request(environment, release, previous_run, accept_uat):
    if environment not in ('uat', 'prod') or not re.fullmatch(r'[0-9a-f]{40}', release):
        raise ValueError('Select UAT or PROD and the full lowercase 40-character release SHA.')
    if not re.fullmatch(r'[1-9][0-9]*', previous_run):
        raise ValueError('Provide the numeric successful DEV/UAT workflow run ID from its URL.')
    if environment == 'prod' and not accept_uat:
        raise ValueError('PROD requires explicit confirmation that human UAT passed for this release.')
    if environment == 'uat' and accept_uat:
        raise ValueError('Leave acceptance unchecked for UAT; human testing follows UAT deployment.')


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class GitHub:
    def __init__(self, repository):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Use an explicit owner/repository.')
        self.repository = repository
        self.base = f'repos/{repository}/actions'

    def api(self, endpoint, binary=False):
        result = subprocess.run(['gh', 'api', '--method', 'GET', endpoint],
                                capture_output=True, check=True, timeout=180)
        return result.stdout if binary else json.loads(result.stdout)

    def run(self, run_id, kind, release=None):
        if not re.fullmatch(r'[1-9][0-9]*', str(run_id)):
            raise ValueError('Invalid evidence run ID.')
        filename, event = WORKFLOWS[kind]
        workflow = self.api(f'{self.base}/workflows/{filename}')
        run = self.api(f'{self.base}/runs/{run_id}')
        if (str(run['id']) != str(run_id) or run['workflow_id'] != workflow['id']
                or run['path'] != f'.github/workflows/{filename}'
                or run['repository']['full_name'] != self.repository
                or run['head_repository']['full_name'] != self.repository
                or run['event'] != event or run['head_branch'] != 'main'
                or run['status'] != 'completed' or run['conclusion'] != 'success'
                or (release is not None and run['head_sha'] != release)):
            raise ValueError(f'{kind.upper()}: evidence must come from the expected successful main workflow.')
        return run

    def artifact(self, run, name, filename, destination, fresh=False):
        artifacts = []
        page = 1
        while True:
            batch = self.api(f'{self.base}/runs/{run["id"]}/artifacts?per_page=100&page={page}')['artifacts']
            artifacts.extend(a for a in batch if a['name'] == name)
            if len(batch) < 100:
                break
            page += 1
        if len(artifacts) != 1 or artifacts[0]['expired']:
            raise ValueError(f'{name}: one unexpired artifact from that run is required.')
        artifact = artifacts[0]
        if fresh and timestamp(artifact['created_at']) < timestamp(run['run_started_at']):
            raise ValueError('Deployment evidence predates the latest workflow attempt.')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', artifact.get('digest') or ''):
            raise ValueError('GitHub artifact digest is missing; evidence cannot be verified.')
        blob = self.api(f'{self.base}/artifacts/{artifact["id"]}/zip', binary=True)
        if 'sha256:' + hashlib.sha256(blob).hexdigest() != artifact['digest']:
            raise ValueError('Downloaded artifact checksum differs from GitHub metadata.')
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            if len(files) != 1 or files[0].filename != filename or files[0].file_size > 512 * 1024 * 1024:
                raise ValueError('Unexpected contents in release/deployment artifact.')
            # Read a named member, never extract arbitrary archive paths.
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(files[0]))
        return {'run_id': str(run['id']), 'run_attempt': run['run_attempt'],
                'artifact_id': artifact['id'], 'artifact_digest': artifact['digest'],
                'workflow': run['path'], 'head_sha': run['head_sha'],
                'receipt_sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}

    def evidence(self, environment, run_id, release, directory):
        run = self.run(run_id, environment, release if environment == 'dev' else None)
        path = directory / f'{environment}-receipt.json'
        reference = self.artifact(run, f'aws-{environment}-{release}',
                                  f'aws-{environment}-receipt.json', path, fresh=True)
        receipt = read_json(path)
        if (receipt.get('status') != 'passed' or receipt.get('environment') != environment
                or receipt.get('release') != release
                or receipt.get('source', {}).get('repository') != self.repository
                or not timestamp(run['run_started_at']) <= timestamp(receipt['completed_at']) <= timestamp(run['updated_at'])):
            raise ValueError('Receipt does not prove the requested release passed the previous environment.')
        if environment == 'uat':
            execution = receipt.get('execution', {})
            if execution != execution_identity(self.repository, run):
                raise ValueError('UAT receipt is not evidence produced by this GitHub workflow attempt.')
            prior = receipt['promotion']['previous']
            dev, actual = self.evidence('dev', prior['run_id'], release, directory)
            if prior != actual:
                raise ValueError('DEV evidence changed since UAT; repeat promotion from verified evidence.')
            same_release(dev, receipt)
        return receipt, reference


def execution_identity(repository, run):
    return {'kind': 'github', 'repository': repository, 'run_id': str(run['id']),
            'run_attempt': run['run_attempt'], 'head_sha': run['head_sha']}


def current_execution(github):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        return {'kind': 'local'}
    run = github.api(f'{github.base}/runs/{os.environ["GITHUB_RUN_ID"]}')
    if (str(run['id']) != os.environ['GITHUB_RUN_ID']
            or run['event'] != 'workflow_dispatch' or run['head_branch'] != 'main'
            or run['path'] != '.github/workflows/promote.yml'
            or run['repository']['full_name'] != github.repository
            or run['head_repository']['full_name'] != github.repository
            or run['head_sha'] != os.environ['GITHUB_SHA']
            or str(run['run_attempt']) != os.environ['GITHUB_RUN_ATTEMPT']):
        raise ValueError('Promotion must execute the selected main-branch workflow revision.')
    return execution_identity(github.repository, run)


def same_release(receipt, data):
    if any(receipt.get(key) != data.get(key) for key in ('release', 'source', 'artifact', 'images')):
        raise ValueError('Previous deployment does not match the CI archive and immutable image digests.')


def verify_previous_environment(aws, receipt):
    """Human acceptance must refer to the same environment session and release."""
    env = receipt['environment']
    name = f'{PROJECT}-{env}'
    foundation = aws.describe(name)
    check_foundation(foundation, aws.account, minimum_minutes=15, environment=env)
    if foundation['StackId'] != receipt['foundation_stack_id']:
        raise ValueError('Previous environment was recreated; deploy and verify this release there again.')
    app = aws.describe(name + '-app')
    if not app or app['StackStatus'] not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
        raise ValueError('Previous environment application is absent, changing or failed.')
    tags = {tag['Key']: tag['Value'] for tag in app.get('Tags', [])}
    values = outputs(app)
    expected = {'ReleaseId': receipt['release'], 'ServiceArn': receipt['service_arn'],
                'ApplicationTaskDefinitionArn': receipt['task_definition_arn'],
                'FrontendImageDigest': receipt['images']['frontend']['digest'],
                'BackendImageDigest': receipt['images']['backend']['digest']}
    if (tags.get('ParentStackId') != foundation['StackId'] or tags.get('Project') != PROJECT
            or any(values.get(key) != value for key, value in expected.items())):
        raise ValueError('Previous environment no longer contains the verified release; repeat its deployment/testing.')


def promote(args):
    check_request(args.environment, args.release, args.previous_run_id, args.accept_uat)
    github = GitHub(args.repository)
    execution = current_execution(github)
    previous_env = 'dev' if args.environment == 'uat' else 'uat'
    print(f'Verifying {previous_env.upper()} run {args.previous_run_id} for {args.release}...', flush=True)
    with tempfile.TemporaryDirectory(prefix='delivery-lab-promotion-') as temporary:
        directory = Path(temporary)
        previous, reference = github.evidence(previous_env, args.previous_run_id, args.release, directory)
        ci_run = str(previous['source']['ci_run_id'])
        ci = github.run(ci_run, 'ci', args.release)
        archive, metadata = directory / 'release.tar.gz', directory / 'aws-release.json'
        github.artifact(ci, f'release-{args.release}', 'release.tar.gz', archive)
        github.artifact(ci, f'aws-release-{args.release}', 'aws-release.json', metadata)
        unpacked = directory / 'release'
        data = validate_release(archive, metadata, unpacked, args.account, args.region,
                                args.release, args.repository, ci_run)
        same_release(previous, data)
        print(f'Verified original CI run {ci_run}, archive checksums and unchanged image metadata.', flush=True)
        aws = Aws(args.account, args.region, args.profile)
        identity = aws.verify()
        role = f'{PROJECT}-github-{args.environment}'
        if args.require_role and not identity['Arn'].startswith(f'arn:aws:sts::{args.account}:assumed-role/{role}/'):
            raise ValueError('Promotion requires the configured temporary GitHub environment role.')
        verify_previous_environment(aws, previous)
        foundation = aws.describe(f'{PROJECT}-{args.environment}')
        values = check_foundation(foundation, aws.account, environment=args.environment)
        check_runner_network(values['AllowedClientCidr'])
        runtime = resolve_images(aws, data)
        if args.verify_only:
            return {'status': 'verified-only', 'environment': args.environment, 'release': args.release,
                    'previous': reference}
        receipt = Deployment(aws, foundation, unpacked, data, args.environment).deploy(runtime)
        receipt.update(execution=execution, promotion={'previous_environment': previous_env,
                       'previous': reference, 'accepted_uat': args.accept_uat,
                       'requested_by': os.environ.get('GITHUB_TRIGGERING_ACTOR') or identity['Arn']})
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', required=True, choices=['uat', 'prod'])
    parser.add_argument('--release', required=True)
    parser.add_argument('--previous-run-id', required=True, help='Successful DEV run for UAT; successful UAT run for PROD.')
    parser.add_argument('--accept-uat', action='store_true')
    parser.add_argument('--repository', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='eu-west-1', choices=['eu-west-1'])
    parser.add_argument('--profile')
    parser.add_argument('--require-role', action='store_true')
    parser.add_argument('--verify-only', action='store_true', help='Check all prerequisites without changing AWS.')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    receipt = {'status': 'failed', 'environment': args.environment, 'release': args.release}
    write_json(args.output, receipt)
    try:
        receipt = promote(args)
    finally:
        write_json(args.output, receipt)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        print(getattr(error, 'stderr', None) or str(error), file=sys.stderr)
        sys.exit(1)
