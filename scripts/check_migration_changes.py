#!/usr/bin/env python3
"""Keep versioned Oracle migrations append-only across the CI merge boundary."""
from pathlib import Path
import re
import subprocess
import sys

MIGRATIONS = 'backend/src/main/resources/db/migration'


def check_changes(base='HEAD^', head='HEAD'):
    # CI fetches both the merge/push commit and its first parent.
    changed = subprocess.run(['git', 'diff', '--name-only', '--no-renames',
                              '--diff-filter=DMRT', base, head, '--', MIGRATIONS],
                             capture_output=True, text=True, check=True).stdout.splitlines()
    if changed:
        raise ValueError('Existing migrations must not be edited, removed or renamed; add a new version: '
                         + ', '.join(changed))
    for path in Path(MIGRATIONS).rglob('*'):
        if path.is_file() and (path.parent != Path(MIGRATIONS) or not re.fullmatch(r'V[0-9]+(?:_[0-9]+)*__[A-Za-z0-9_]+\.sql', path.name)):
            raise ValueError('Recovery supports flat versioned SQL migrations only: ' + str(path))
    print('Versioned Oracle migrations are append-only.')


if __name__ == '__main__':
    try:
        check_changes()
    except (ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
