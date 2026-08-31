#!/bin/sh
# Fix ownership on the bind-mounted config volume, then drop root.
#
# TrueNAS datasets are usually owned by a specific uid/gid (often apps:apps or a
# user you created), so PUID/PGID let the container write there without you
# having to chmod 777 anything.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
CONFIG_DIR="${CONFIG_DIR:-/config}"

if [ "$(id -u)" != "0" ]; then
    # Already running as a non-root user (e.g. compose set `user:`). Nothing to
    # drop, so just go.
    exec "$@"
fi

mkdir -p "$CONFIG_DIR"

# Only chown when it is actually wrong -- recursively chowning a large config
# directory on every boot is wasted I/O on a NAS.
current_uid="$(stat -c '%u' "$CONFIG_DIR")"
current_gid="$(stat -c '%g' "$CONFIG_DIR")"
if [ "$current_uid" != "$PUID" ] || [ "$current_gid" != "$PGID" ]; then
    echo "[entrypoint] chown $PUID:$PGID $CONFIG_DIR"
    chown -R "$PUID:$PGID" "$CONFIG_DIR" || \
        echo "[entrypoint] WARNING: could not chown $CONFIG_DIR; if the app cannot write its database, fix the ownership of the host directory to $PUID:$PGID"
fi

echo "[entrypoint] starting as uid=$PUID gid=$PGID"
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
