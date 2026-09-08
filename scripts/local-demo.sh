#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/lab.py doctor
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
./backend/mvnw -f backend/pom.xml -B -ntp verify
python3 -m unittest discover -s tests -v
release="local-$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
mkdir -p .local/packages
python3 scripts/package.py --release "$release" --output ".local/packages/$release.tar.gz"
python3 scripts/lab.py init
python3 scripts/lab.py install --archive ".local/packages/$release.tar.gz"
python3 scripts/lab.py deploy --release "$release" --environment dev
echo "Release: $release"
echo "Next, manually promote with: python3 scripts/lab.py deploy --release $release --environment uat"
