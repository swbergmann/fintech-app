#!/usr/bin/env python3
"""Deploy the verified CI release to AWS DEV using the shared deployment engine."""
import subprocess
import sys
from aws_deployment import main

if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(getattr(error, 'stderr', None) or str(error), file=sys.stderr)
        sys.exit(1)
