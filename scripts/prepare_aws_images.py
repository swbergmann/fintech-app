#!/usr/bin/env python3
"""Wrap an already-built release in local Linux/amd64 images for ECS. Never push or deploy."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

from lab import extract_release, locked, now, read_json, sha256, verify_release, write_json

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = 'linux/amd64'


def inspect(tag):
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag], text=True))[0]
    if (image['Os'], image['Architecture']) != ('linux', 'amd64'):
        raise ValueError(f'{tag} is not Linux/amd64 as required by the ECS task definitions.')
    return image['Id']


def prepare(archive, home):
    home = Path(home).resolve()
    with locked(home), tempfile.TemporaryDirectory(dir=home) as temporary:
        directory = Path(temporary)
        extract_release(archive, directory)
        manifest = verify_release(directory)
        if not (directory / 'infra/15-validate-upstream.sh').is_file():
            raise ValueError('Use a release containing the AWS-ready frontend entrypoint.')
        release = manifest['release']
        digest = sha256(directory / 'manifest.json')
        tags = {service: f'delivery-lab-aws/{service}:{release}' for service in ('backend', 'frontend')}
        receipt_path = home / f'{release}.json'
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            if receipt['manifest_sha256'] != digest:
                raise ValueError('This release ID already refers to different contents; use a new release ID.')
            current = {service: inspect(tag) for service, tag in tags.items()}
            if current != receipt['images']:
                raise ValueError('Prepared images changed; do not rebuild a release for promotion.')
            print(f'Reusing prepared images for {release}.')
            return receipt
        images = {}
        for service, tag in tags.items():
            subprocess.run(['docker', 'build', '--platform', PLATFORM,
                            '--label', f'org.opencontainers.image.revision={release}',
                            '--label', f'com.delivery-lab.manifest-sha256={digest}',
                            '-f', str(directory / f'infra/{service}.Dockerfile'),
                            '-t', tag, str(directory)], check=True)
            images[service] = inspect(tag)
        receipt = {'release': release, 'manifest_sha256': digest, 'platform': PLATFORM,
                   'tags': tags, 'images': images, 'prepared_at': now()}
        write_json(receipt_path, receipt)
        print(f'Prepared {release}; image IDs recorded in {receipt_path}. Nothing was pushed or deployed.')
        return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--home', type=Path, default=ROOT / '.local/aws-images')
    args = parser.parse_args()
    prepare(args.archive, args.home)
