#!/bin/sh
# Run as PUID:PGID (default 1000:1000) and make sure /data is writable by it.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA="${SAVEPOINT_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA" "${XDG_CACHE_HOME:-/tmp/cache}"
    # Only walk the tree when ownership is actually wrong (first start or PUID change).
    if [ "$(stat -c %u:%g "$DATA")" != "$PUID:$PGID" ]; then
        chown -R "$PUID:$PGID" "$DATA"
    fi
    chown "$PUID:$PGID" "${XDG_CACHE_HOME:-/tmp/cache}"
    export HOME="$DATA"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

exec "$@"
