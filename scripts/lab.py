#!/usr/bin/env python3
"""Local deployment runner. Requires Python 3.10+, Docker Compose v2+, macOS or Linux."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import string
import subprocess
import sys
import tarfile
import tempfile
from smoke import smoke

ROOT = Path(__file__).resolve().parents[1]
PORTS = {'dev': 8080, 'uat': 8081, 'prod': 8082}

def valid_release(release):
    if not re.fullmatch(r'(?:[a-f0-9]{40}|local-[a-f0-9]{12})', release):
        raise ValueError('Release must be a full lowercase Git SHA or local- plus 12 hex digits.')
    return release

def lab_home():
    configured = os.environ.get('LAB_HOME')
    if os.environ.get('GITHUB_ACTIONS') == 'true' and not configured:
        raise ValueError('Set the LAB_HOME repository variable to an absolute directory outside the runner checkout.')
    path = Path(configured) if configured else ROOT / '.local'
    if not path.is_absolute(): raise ValueError('LAB_HOME must be an absolute path.')
    return path.resolve()

def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''): digest.update(chunk)
    return digest.hexdigest()

def read_json(path):
    return json.loads(Path(path).read_text())

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)

def now(): return datetime.now(timezone.utc).isoformat()

def run(command, **kwargs):
    # Never print commands containing resolved database credentials.
    return subprocess.run(command, check=True, text=True, **kwargs)

def docker_ready():
    try:
        run(['docker', 'info'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        run(['docker', 'compose', 'version'], stdout=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise ValueError('Docker is not ready. Start Docker Desktop or Colima, then check docker info and docker compose version.') from error

@contextmanager
def locked(home):
    home.mkdir(parents=True, exist_ok=True)
    with (home / '.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield

def initialize(home):
    directory = home / 'env'
    directory.mkdir(parents=True, exist_ok=True)
    for environment, port in PORTS.items():
        path = directory / f'{environment}.env'
        if path.exists(): continue  # Never rotate credentials behind an existing Oracle volume.
        def password():
            return 'Lab9' + ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(28))
        content = (f'APP_ENV={environment.upper()}\nHTTP_PORT={port}\n'
                   f'DB_ADMIN_PASSWORD={password()}\nAPP_DB_PASSWORD={password()}\n')
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle: handle.write(content)
    print(f'Environment settings ready in {directory}. Existing passwords were preserved.')

def extract_release(archive, directory):
    with tarfile.open(archive, 'r:gz') as bundle:
        members = bundle.getmembers()
        names = set()
        for member in members:
            path = Path(member.name)
            if (not member.isfile() or path.is_absolute() or '..' in path.parts
                    or '\\' in member.name or member.name in names):
                raise ValueError('Unsafe or duplicate file in release archive.')
            names.add(member.name)
        # Explicit allow-list validation above works on supported Python versions.
        for member in members:
            destination = directory / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, destination.open('wb') as target:
                shutil.copyfileobj(source, target)
            destination.chmod(0o755 if member.mode & 0o111 else 0o644)

def verify_release(directory):
    manifest = read_json(directory / 'manifest.json')
    valid_release(manifest['release'])
    expected = manifest['checksums']
    actual = {str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file()}
    if actual != set(expected) | {'manifest.json'}:
        raise ValueError('Release contents differ from the manifest.')
    required = {'backend.jar', 'frontend/index.html', 'frontend/release.json',
                'infra/compose.yaml', 'infra/backend.Dockerfile', 'infra/frontend.Dockerfile',
                'infra/nginx.conf', 'infra/init-db.sh'}
    if not required <= set(expected): raise ValueError('Required release files are missing.')
    for name, checksum in expected.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or sha256(directory / path) != checksum:
            raise ValueError(f'Release checksum failed: {name}')
    if read_json(directory / 'frontend/release.json')['release'] != manifest['release']:
        raise ValueError('Frontend release metadata does not match the manifest.')
    return manifest

def image_tags(release):
    return {'backend': f'delivery-lab-backend:{release}', 'frontend': f'delivery-lab-frontend:{release}'}

def image_ids(release):
    return {service: run(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag],
                         capture_output=True).stdout.strip()
            for service, tag in image_tags(release).items()}

def install(home, archive):
    docker_ready()
    releases = home / 'releases'
    releases.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=releases) as temporary:
        staging = Path(temporary)
        extract_release(archive, staging)
        manifest = verify_release(staging)
        release = manifest['release']
        destination = releases / release
        installation_path = home / 'installed' / f'{release}.json'
        digest = sha256(staging / 'manifest.json')
        if installation_path.exists():
            installed = read_json(installation_path)
            verify_release(destination)
            if installed['manifest_sha256'] != digest or sha256(destination / 'manifest.json') != digest:
                raise ValueError('An installed release ID cannot be reused with different contents.')
            if image_ids(release) != installed['images']:
                raise ValueError('Installed Docker images changed. Restore them; do not rebuild a promoted release.')
            print(f'Release {release} already installed; reusing its images.')
            return
        if destination.exists():
            if sha256(destination / 'manifest.json') != digest:
                raise ValueError('An incomplete installation has different contents for this release ID.')
            verify_release(destination)
        else: shutil.copytree(staging, destination)
        tags = image_tags(release)
        for service, tag in tags.items():
            print(f'Creating {service} runtime image for {release}…', flush=True)
            run(['docker', 'build', '-f', str(destination / f'infra/{service}.Dockerfile'),
                 '-t', tag, str(destination)])
        write_json(installation_path, {'release': release, 'manifest_sha256': digest,
                   'images': image_ids(release), 'installed_at': now()})
        print(f'Installed {release}. The same runtime images will be used in DEV, UAT and PROD.')

def check_promotion(home, release, environment, accept_uat=False):
    valid_release(release)
    if environment not in PORTS: raise ValueError('Unknown environment.')
    installed = read_json(home / 'installed' / f'{release}.json')
    if environment != 'dev':
        previous = 'dev' if environment == 'uat' else 'uat'
        receipt_path = home / 'history' / release / f'{previous}.json'
        if not receipt_path.exists(): raise ValueError(f'This release must pass {previous.upper()} first.')
        receipt = read_json(receipt_path)
        if (receipt.get('status') != 'passed' or receipt.get('release') != release
                or receipt.get('manifest_sha256') != installed['manifest_sha256']
                or receipt.get('images') != installed['images']):
            raise ValueError('Previous environment evidence does not match this release.')
    if environment == 'prod' and not accept_uat:
        raise ValueError('Record successful human UAT with --accept-uat before deploying PROD.')
    return installed

def compose(home, release, environment, *arguments):
    tags = image_tags(release)
    settings = home / 'env' / f'{environment}.env'
    if not settings.exists(): raise ValueError('Run lab.py init first.')
    environment_variables = dict(os.environ, RELEASE_ID=release,
                                 BACKEND_IMAGE=tags['backend'], FRONTEND_IMAGE=tags['frontend'])
    command = ['docker', 'compose', '--project-name', f'deliverylab-{environment}',
               '--env-file', str(settings), '-f', str(home / 'releases' / release / 'infra/compose.yaml')]
    return run(command + list(arguments), env=environment_variables)

def deploy(home, release, environment, accept_uat=False):
    installed = check_promotion(home, release, environment, accept_uat)
    directory = home / 'releases' / release
    verify_release(directory)
    if sha256(directory / 'manifest.json') != installed['manifest_sha256']:
        raise ValueError('Installed release manifest was modified.')
    docker_ready()
    if image_ids(release) != installed['images']: raise ValueError('Runtime image IDs changed.')
    receipt = dict(installed, environment=environment, actor=os.environ.get('GITHUB_ACTOR', 'local-user'),
                   workflow_run=os.environ.get('GITHUB_RUN_ID'), started_at=now(),
                   human_uat_accepted=bool(accept_uat))
    # Also remember the attempted release, so a failed environment can be stopped safely.
    write_json(home / 'state' / f'{environment}-attempt.json', receipt)
    try:
        print(f'Deploying {release} to {environment.upper()}…', flush=True)
        compose(home, release, environment, 'up', '-d', '--wait', '--wait-timeout', '900', 'db')
        compose(home, release, environment, 'run', '--rm', '--no-deps', 'migrate')
        compose(home, release, environment, 'up', '-d', '--wait', '--wait-timeout', '240', 'backend', 'frontend')
        smoke(f'http://127.0.0.1:{PORTS[environment]}', release, environment)
    except Exception:
        receipt.update(status='failed', finished_at=now())
        write_json(home / 'failures' / f'{environment}-{release}.json', receipt)
        # Re-running a previously passed release and failing invalidates its old pass.
        (home / 'history' / release / f'{environment}.json').unlink(missing_ok=True)
        raise
    receipt.update(status='passed', finished_at=now())
    write_json(home / 'history' / release / f'{environment}.json', receipt)
    write_json(home / 'state' / f'{environment}.json', receipt)
    print(f'{environment.upper()} ready at http://127.0.0.1:{PORTS[environment]}')

def status(home):
    for environment, port in PORTS.items():
        path = home / 'state' / f'{environment}.json'
        if path.exists():
            receipt = read_json(path)
            print(f'{environment.upper():4}  last successful release: {receipt["release"]}  http://127.0.0.1:{port}')
        else: print(f'{environment.upper():4}  no successful deployment recorded')
    print('These are deployment receipts, not a live health check. Use Docker or /api/health for current health.')

def stop(home, environment):
    path = home / 'state' / f'{environment}-attempt.json'
    if not path.exists(): raise ValueError('This environment has no deployment attempt to stop.')
    release = read_json(path)['release']
    compose(home, release, environment, 'down')  # Intentionally retains the Oracle volume.
    print(f'Stopped {environment.upper()}; database records were retained.')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    commands.add_parser('init')
    commands.add_parser('status')
    install_parser = commands.add_parser('install')
    install_parser.add_argument('--archive', type=Path, required=True)
    deploy_parser = commands.add_parser('deploy')
    deploy_parser.add_argument('--release', required=True)
    deploy_parser.add_argument('--environment', choices=PORTS, required=True)
    deploy_parser.add_argument('--accept-uat', action='store_true')
    stop_parser = commands.add_parser('stop')
    stop_parser.add_argument('--environment', choices=PORTS, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'doctor':
            docker_ready(); print('Docker and Compose are ready.'); return
        home = lab_home()
        with locked(home):
            if args.command == 'init': initialize(home)
            elif args.command == 'install': install(home, args.archive)
            elif args.command == 'deploy': deploy(home, args.release, args.environment, args.accept_uat)
            elif args.command == 'status': status(home)
            elif args.command == 'stop': stop(home, args.environment)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__': main()
