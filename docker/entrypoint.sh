#!/bin/sh
set -eu

: "${PAPERLESS_GID:=10000}"
: "${SHARE_NAME:=inbox}"

if ! getent group paperless >/dev/null; then
    groupadd --gid "$PAPERLESS_GID" paperless
fi

mkdir -p /shares/inbox /run/samba

export SHARE_NAME
envsubst '${SHARE_NAME}' </etc/samba/smb.conf.template >/etc/samba/smb.conf
testparm -s /etc/samba/smb.conf >/dev/null

(
    while true; do
        smb-sync run || echo "smb-sync daemon exited, restarting in 5s" >&2
        sleep 5
    done
) &

exec smbd --foreground --no-process-group --debug-stdout
