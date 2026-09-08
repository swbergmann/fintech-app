#!/usr/bin/env python3
"""Package already-built application artifacts; do not build during promotion."""
import argparse
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
from lab import valid_release, sha256

ROOT = Path(__file__).resolve().parents[1]

def package(release, output):
    valid_release(release)
    jar = ROOT / 'backend/target/delivery-lab.jar'
    frontend = ROOT / 'frontend/dist'
    if not jar.is_file() or not (frontend / 'index.html').is_file():
        raise ValueError('Build the frontend and backend before packaging.')
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary)
        shutil.copy2(jar, staging / 'backend.jar')
        shutil.copytree(frontend, staging / 'frontend')
        shutil.copytree(ROOT / 'infra', staging / 'infra')
        (staging / 'frontend/release.json').write_text(json.dumps({'release': release}) + '\n')
        checksums = {str(p.relative_to(staging)): sha256(p)
                     for p in sorted(staging.rglob('*')) if p.is_file()}
        (staging / 'manifest.json').write_text(json.dumps(
            {'release': release, 'checksums': checksums}, indent=2) + '\n')
        with tarfile.open(output, 'w:gz') as archive:
            for path in sorted(staging.rglob('*')):
                if path.is_file(): archive.add(path, arcname=str(path.relative_to(staging)))
    print(f'Packaged {release}: {output}')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    package(args.release, args.output)
