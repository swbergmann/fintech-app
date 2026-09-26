#!/bin/sh
set -eu
# The entrypoint renders the template after this check. Accept only host:port,
# never URLs, paths or text that could become extra Nginx directives.
if ! printf '%s\n' "$BACKEND_UPSTREAM" | awk '
    /^[A-Za-z0-9][A-Za-z0-9.-]*:[0-9]+$/ {
        split($0, parts, ":")
        if (parts[2] >= 1 && parts[2] <= 65535) valid = 1
    }
    END { exit !(NR == 1 && valid) }
'; then
    echo 'BACKEND_UPSTREAM must be a hostname or IPv4 address followed by a port (1–65535).' >&2
    exit 1
fi
