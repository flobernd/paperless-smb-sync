#!/bin/sh
set -eu

: "${PAPERLESS_GID:=10000}"
: "${SHARE_NAME:=inbox}"

# the loop below restarts a daemon that fails forever, so a bad setting would
# otherwise leave smbd running beside a daemon that never syncs
if ! smb-sync check; then
    echo "smb-sync configuration is not valid, refusing to start" >&2
    exit 1
fi

if ! getent group paperless >/dev/null; then
    groupadd --gid "$PAPERLESS_GID" paperless
fi

mkdir -p /shares/inbox /run/samba /etc/samba/conf.d

export SHARE_NAME
# shellcheck disable=SC2016 # envsubst takes the name literally and replaces only that variable
envsubst '${SHARE_NAME}' </etc/samba/smb.conf.template >/etc/samba/smb.conf

# Appended after the managed share so a [global] header in a drop-in re-opens
# the global section and the user's settings win over the defaults.
for dropin in /etc/samba/conf.d/*.conf; do
    [ -e "$dropin" ] || continue
    echo "loading samba drop-in $dropin" >&2
    printf '\n' >>/etc/samba/smb.conf
    cat "$dropin" >>/etc/samba/smb.conf
done

if ! parse=$(testparm -s /etc/samba/smb.conf 2>&1 >/dev/null); then
    echo "$parse" >&2
    echo "samba configuration failed to load, refusing to start" >&2
    exit 1
fi
echo "$parse" >&2

# testparm exits 0 on an unknown parameter and only warns, so a typo in a
# drop-in would otherwise be ignored silently for the life of the container.
case "$parse" in
    *"Unknown parameter"*)
        echo "smb.conf contains an unknown parameter, refusing to start" >&2
        exit 1
        ;;
esac

# passdb is on the volume, but the unix accounts that smbd needs for a login
# are gone after a recreation of the container. A failure does not stop the
# start, since the first cycle repairs what is left.
if ! smb-sync restore; then
    echo "could not restore every unix account from passdb" >&2
fi

(
    while true; do
        smb-sync run || echo "smb-sync daemon exited, restarting in 5s" >&2
        sleep 5
    done
) &

exec smbd --foreground --no-process-group --debug-stdout
