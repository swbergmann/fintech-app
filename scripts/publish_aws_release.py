#!/usr/bin/env python3
"""Publish tested outputs to ECR once and record repository digests for promotion."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from lab import extract_release, sha256, verify_release, write_json
from prepare_aws_images import PLATFORM, build_image

SERVICES = ('backend', 'frontend')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


class Registry:
    def __init__(self, account, region, project):
        if not re.fullmatch(r'[0-9]{12}', account):
            raise ValueError('An explicit 12-digit AWS account is required.')
        if region != 'eu-west-1':
            raise ValueError('This lab publishes only in eu-west-1.')
        if not re.fullmatch(r'[a-z][a-z0-9-]{1,24}', project):
            raise ValueError('Invalid project name.')
        self.account, self.region, self.project = account, region, project
        self.host = f'{account}.dkr.ecr.{region}.amazonaws.com'

    def aws(self, *arguments, raw=False):
        result = subprocess.run(['aws', '--region', self.region, '--no-cli-pager',
                                 *arguments, '--output', 'text' if raw else 'json'],
                                check=True, capture_output=True, text=True)
        return result.stdout.strip() if raw else json.loads(result.stdout)

    def repositories(self):
        if self.aws('sts', 'get-caller-identity')['Account'] != self.account:
            raise ValueError('AWS credentials belong to another account.')
        names = [f'{self.project}/{service}' for service in SERVICES]
        repositories = self.aws('ecr', 'describe-repositories', '--registry-id', self.account,
                                '--repository-names', *names)['repositories']
        by_name = {repo['repositoryName']: repo for repo in repositories}
        for name in names:
            repo = by_name[name]
            if (repo['repositoryUri'] != f'{self.host}/{name}' or
                    repo['imageTagMutability'] != 'IMMUTABLE' or
                    repo.get('imageTagMutabilityExclusionFilters')):
                raise ValueError('Publishing requires the expected ECR repositories with immutable tags.')
        return {service: f'{self.host}/{self.project}/{service}' for service in SERVICES}

    def digest(self, service, release):
        # BatchGetImage reports a missing tag without hiding permissions/network errors.
        result = self.aws('ecr', 'batch-get-image', '--registry-id', self.account,
                          '--repository-name', f'{self.project}/{service}',
                          '--image-ids', f'imageTag={release}')
        if result.get('failures'):
            failures = result['failures']
            if (len(failures) == 1 and failures[0]['failureCode'] == 'ImageNotFound'
                    and not result.get('images')):
                return None
            raise ValueError(f'ECR image lookup failed: {[f["failureCode"] for f in failures]}')
        images = result.get('images', [])
        if len(images) != 1 or not DIGEST.fullmatch(images[0]['imageId']['imageDigest']):
            raise ValueError('ECR did not return one valid repository digest.')
        return images[0]['imageId']['imageDigest']


@contextmanager
def authenticated_docker(registry):
    # An ephemeral config prevents the ECR token from entering the user's Docker
    # config or credential helper. Preserve the local daemon (including Colima).
    endpoint = os.environ.get('DOCKER_HOST')
    if not endpoint:
        context = json.loads(subprocess.check_output(['docker', 'context', 'inspect'], text=True))[0]
        endpoint = context['Endpoints']['docker']['Host']
    if not endpoint.startswith('unix://'):
        raise ValueError('Use a local Docker daemon on this Mac or the Linux CI runner.')
    with tempfile.TemporaryDirectory(prefix='delivery-lab-ecr-') as temporary:
        docker = ('docker', '--config', temporary, '--host', endpoint)
        password = registry.aws('ecr', 'get-login-password', raw=True)
        subprocess.run([*docker, 'login', '--username', 'AWS', '--password-stdin', registry.host],
                       input=password + '\n', text=True, check=True, capture_output=True)
        del password
        yield docker
        # The temporary directory, including its token, is removed even on failure.


def verify_image(docker, reference, release, manifest_digest):
    subprocess.run([*docker, 'pull', '--platform', PLATFORM, reference], check=True)
    image = json.loads(subprocess.check_output([*docker, 'image', 'inspect', reference], text=True))[0]
    labels = image.get('Config', {}).get('Labels') or {}
    if ((image['Os'], image['Architecture']) != ('linux', 'amd64') or
            labels.get('org.opencontainers.image.revision') != release or
            labels.get('com.delivery-lab.manifest-sha256') != manifest_digest):
        raise ValueError('Stored image differs from this tested release; use a new commit, not an overwritten tag.')
    return image['Id']


def publish(archive, output, registry, release, repository, run_id, docker):
    if not re.fullmatch(r'[0-9a-f]{40}', release):
        raise ValueError('AWS releases require a full 40-character commit SHA.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not re.fullmatch(r'[1-9][0-9]*', run_id):
        raise ValueError('Provide the source GitHub repository and successful CI run ID.')
    # No success document may survive a failed retry at the same output path.
    output = Path(output)
    if output.resolve() == Path(archive).resolve():
        raise ValueError('Output must differ from the input archive.')
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='delivery-lab-release-') as temporary:
        directory = Path(temporary)
        extract_release(archive, directory)
        manifest = verify_release(directory)
        if manifest['release'] != release:
            raise ValueError('Archive release does not match the expected source commit.')
        if not (directory / 'infra/15-validate-upstream.sh').is_file():
            raise ValueError('Use a release containing the AWS-ready frontend entrypoint.')
        manifest_digest = sha256(directory / 'manifest.json')
        repositories = registry.repositories()
        stored = {}
        missing = []
        # Validate every existing image before pushing anything. This also allows
        # a failed frontend push to resume without rebuilding the stored backend.
        for service in SERVICES:
            digest = registry.digest(service, release)
            if digest:
                uri = f'{repositories[service]}@{digest}'
                image_id = verify_image(docker, uri, release, manifest_digest)
                stored[service] = {'repository': repositories[service], 'tag': release,
                                   'digest': digest, 'uri': uri, 'image_id': image_id}
            else:
                missing.append(service)
        for service in missing:
            tag = f'{repositories[service]}:{release}'
            image_id = build_image(directory, service, release, manifest_digest, tag, docker)
            subprocess.run([*docker, 'push', tag], check=True)
            digest = registry.digest(service, release)
            if not digest:
                raise ValueError('Published image was not found in ECR.')
            uri = f'{repositories[service]}@{digest}'
            if verify_image(docker, uri, release, manifest_digest) != image_id:
                raise ValueError('Published image differs from the image just prepared.')
            stored[service] = {'repository': repositories[service], 'tag': release,
                               'digest': digest, 'uri': uri, 'image_id': image_id}
        descriptor = {
            'schema_version': 1, 'release': release, 'platform': PLATFORM,
            'aws_account': registry.account, 'aws_region': registry.region,
            'source': {'repository': repository, 'ci_run_id': run_id,
                       'ci_run_url': f'https://github.com/{repository}/actions/runs/{run_id}'},
            'artifact': {'name': f'release-{release}', 'file': 'release.tar.gz',
                         'archive_sha256': sha256(archive), 'manifest_sha256': manifest_digest},
            'images': stored,
        }
        write_json(output, descriptor)
        print(f'Release {release}: both ECR digests verified and stored in {output}.')
        return descriptor


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='eu-west-1')
    parser.add_argument('--project', default='delivery-lab')
    parser.add_argument('--release', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    registry = Registry(args.account, args.region, args.project)
    with authenticated_docker(registry) as docker:
        publish(args.archive, args.output, registry, args.release, args.repository, args.run_id, docker)
