#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

API=http://localhost:8000
compose() { docker compose -f compose.yaml "$@"; }
api() {
    curl -fsS -H "Authorization: Token $TOKEN" -H 'Content-Type: application/json' "$@"
}
fail() { echo "FAIL: $1" >&2; exit 1; }

cleanup() { compose down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "== starting paperless =="
compose up -d redis paperless

echo "== waiting for the paperless API =="
for i in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$API/api/" || true)
    if [ "$code" != "000" ] && [ "$code" -lt 500 ]; then break; fi
    [ "$i" = 60 ] && fail "paperless did not come up"
    sleep 5
done

TOKEN=$(curl -fsS -X POST "$API/api/token/" -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"adminpass"}' | jq -r .token)
[ -n "$TOKEN" ] && [ "$TOKEN" != "null" ] || fail "could not obtain API token"

echo "== creating group and user =="
GROUP_ID=$(api -X POST "$API/api/groups/" -d '{"name":"smb","permissions":[]}' | jq -r .id)
USER_ID=$(api -X POST "$API/api/users/" \
    -d "{\"username\":\"carol\",\"password\":\"paperless-pass\",\"groups\":[$GROUP_ID]}" | jq -r .id)

echo "== starting smb container =="
SMOKE_TOKEN="$TOKEN" compose up -d --build smb
for i in $(seq 1 12); do
    if compose exec -T smb pdbedit -L 2>/dev/null | grep -q "^carol:"; then break; fi
    [ "$i" = 12 ] && fail "samba account did not appear within 60s"
    sleep 5
done

echo "== asserting provisioning =="
compose exec -T smb pdbedit -L | grep -q "^carol:" || fail "samba account missing"
PERMS=$(compose exec -T smb stat -c '%a %u' /shares/inbox/carol)
echo "$PERMS" | grep -q "^2770 " || fail "folder permissions wrong: $PERMS"
api "$API/api/workflows/" | jq -r '.results[].name' | grep -qx '\[smb-sync\] carol' \
    || fail "workflow missing"

echo "== asserting smb login and write =="
# $NF because compose prefixes each log line with the service name
PASSWORD=$(compose logs smb | grep -A4 "SMB credentials for 'carol'" \
    | awk '/Password:/{print $NF}' | tail -1)
[ -n "$PASSWORD" ] || fail "credentials block not found in logs"
compose exec -T -e SMBPASS="$PASSWORD" smb sh -c \
    'echo smoke >/tmp/smoke.txt && smbclient //localhost/inbox -U "carol%$SMBPASS" -c "put /tmp/smoke.txt smoke.txt"' \
    || fail "smb login or write failed"
compose exec -T smb test -f /shares/inbox/carol/smoke.txt || fail "file not in user folder"

echo "== asserting regen =="
compose exec -T smb smb-sync regen carol | grep -q "Password:" || fail "regen failed"

echo "== asserting deprovisioning =="
api -X PATCH "$API/api/users/$USER_ID/" -d '{"groups":[]}' >/dev/null
for i in $(seq 1 12); do
    if ! compose exec -T smb pdbedit -L 2>/dev/null | grep -q "^carol:"; then break; fi
    [ "$i" = 12 ] && fail "samba account not removed within 60s"
    sleep 5
done
if compose exec -T smb pdbedit -L | grep -q "^carol:"; then fail "samba account not removed"; fi
if compose exec -T smb test -e /shares/inbox/carol; then fail "folder not removed"; fi
api "$API/api/workflows/" | jq -r '.results[].name' | grep -qx '\[smb-sync\] carol' \
    && fail "workflow not removed"

echo "SMOKE TEST PASSED"
