#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

API=http://localhost:${SMOKE_PORT:-8000}
compose() { docker compose -f compose.yaml "$@"; }
api() {
    curl -fsS -H "Authorization: Token $TOKEN" -H 'Content-Type: application/json' "$@"
}
fail() { echo "FAIL: $1" >&2; exit 1; }

wait_for() {
    local what=$1 try
    shift
    for try in $(seq 1 24); do
        "$@" && return
        [ "$try" = 24 ] || sleep 5
    done
    fail "$what"
}

as_user() {
    local token
    token=$(curl -fsS -X POST "$API/api/token/" -H 'Content-Type: application/json' \
        -d "$(jq -n --arg username "$1" --arg password "$2" '{$username, $password}')" \
        | jq -er .token) || return
    shift 2
    curl -fsS -H "Authorization: Token $token" "$@"
}

# grep -q stops reading at the first match, and pipefail would then fail a
# matching check on the SIGPIPE of the writer, so output is captured first.
samba_accounts() { compose exec -T smb pdbedit -L 2>/dev/null; }
has_account() {
    local accounts
    accounts=$(samba_accounts) || return
    grep -q "^$1:" <<<"$accounts"
}
# a pdbedit that fails lists nothing, which must not pass for a removed account
lacks_account() {
    local accounts
    accounts=$(samba_accounts) || return
    ! grep -q "^$1:" <<<"$accounts"
}
one_document_owned() {
    local documents
    documents=$(api "$API/api/documents/?title__iexact=$1") || return
    jq -e --argjson owner "$2" '.count == 1 and .results[0].owner == $owner' \
        <<<"$documents" >/dev/null
}
one_document() {
    local documents
    documents=$(api "$API/api/documents/?title__iexact=$1") || return
    jq -e '.count == 1' <<<"$documents" >/dev/null
}
# paperless adds up the rights of every workflow that matches a scan, so a
# second matching workflow shows up as a second user with the view right
owned_alone() {
    local documents
    documents=$(api "$API/api/documents/?title__iexact=$1&full_perms=true") || return
    jq -e --argjson user "$2" '.count == 1 and .results[0].owner == $user
        and .results[0].permissions.view.users == [$user]' <<<"$documents" >/dev/null
}
has_workflow() {
    local workflows
    workflows=$(api "$API/api/workflows/") || return
    jq -e --arg name "[smb-sync] $1" 'any(.results[]; .name == $name)' \
        <<<"$workflows" >/dev/null
}

cleanup() { compose down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

if [ -z "${SMOKE_IMAGE:-}" ]; then
    echo "== building the image =="
    docker build -t paperless-smb-sync:smoke ../..
    export SMOKE_IMAGE=paperless-smb-sync:smoke
fi

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
if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then fail "could not obtain API token"; fi
export SMOKE_TOKEN=$TOKEN

echo "== creating group and user =="
GROUP_ID=$(api -X POST "$API/api/groups/" \
    -d '{"name":"smb","permissions":["view_document","change_document","delete_document"]}' \
    | jq -r .id)
USER_ID=$(api -X POST "$API/api/users/" \
    -d "{\"username\":\"carol\",\"password\":\"paperless-pass\",\"groups\":[$GROUP_ID]}" | jq -r .id)

echo "== starting smb container =="
compose up -d smb
wait_for "samba account did not appear" has_account carol

echo "== asserting provisioning =="
has_account carol || fail "samba account missing"
PERMS=$(compose exec -T smb stat -c '%a %u' /shares/inbox/carol)
grep -q "^2770 " <<<"$PERMS" || fail "folder permissions wrong: $PERMS"
WORKFLOWS=$(api "$API/api/workflows/" | jq -r '.results[].name') || fail "could not list workflows"
grep -qx '\[smb-sync\] carol' <<<"$WORKFLOWS" || fail "workflow missing"

echo "== asserting smb login and write =="
# $NF because compose prefixes each log line with the service name
PASSWORD=$(compose logs smb | grep -A4 "SMB credentials for 'carol'" \
    | awk '/Password:/{print $NF}' | tail -1) || true
[ -n "$PASSWORD" ] || fail "credentials block not found in logs"
# shellcheck disable=SC2016 # $SMBPASS expands in the shell of the container
compose exec -T -e SMBPASS="$PASSWORD" smb sh -c \
    'echo smoke >/tmp/smoke.txt && smbclient //localhost/inbox -U "carol%$SMBPASS" -c "put /tmp/smoke.txt smoke.txt"' \
    || fail "smb login or write failed"
wait_for "smoke.txt was not imported as a document of carol" one_document_owned smoke "$USER_ID"

echo "== asserting regen =="
REGEN=$(compose exec -T smb smb-sync regen carol) || fail "regen failed"
grep -q "Password:" <<<"$REGEN" || fail "regen failed"

echo "== asserting the owner of a scan in a subfolder =="
DAVE_ID=$(api -X POST "$API/api/users/" \
    -d "{\"username\":\"dave\",\"password\":\"paperless-pass\",\"groups\":[$GROUP_ID]}" | jq -r .id)
wait_for "inbox folder of dave did not appear" compose exec -T smb test -d /shares/inbox/dave
DAVE_PASSWORD=$(compose logs smb | grep -A4 "SMB credentials for 'dave'" \
    | awk '/Password:/{print $NF}' | tail -1) || true
[ -n "$DAVE_PASSWORD" ] || fail "credentials block of dave not found in logs"
# a scan that reaches paperless before the workflow exists gets no owner
wait_for "workflow of dave did not appear" has_workflow dave
# paperless misses a file written before its watcher has caught up with a new
# folder, so the folder is made in a call of its own
compose exec -T smb mkdir -m 2770 /shares/inbox/dave/carol \
    || fail "could not make the subfolder"
compose exec -T smb sh -c 'echo "own scan" >/shares/inbox/dave/own.txt &&
    echo "nested scan" >/shares/inbox/dave/carol/nested.txt' \
    || fail "could not write the scans of dave"
wait_for "nested.txt was not imported" one_document nested
wait_for "own.txt was not imported" one_document own
owned_alone own "$DAVE_ID" || fail "own.txt is not owned by dave alone"
owned_alone nested "$DAVE_ID" || fail "nested.txt is not owned by dave alone"
SEEN=$(as_user dave paperless-pass "$API/api/documents/?title__iexact=nested") \
    || fail "dave could not list documents"
jq -e '.count == 1' <<<"$SEEN" >/dev/null || fail "dave does not see nested.txt"
SEEN=$(as_user carol paperless-pass "$API/api/documents/?title__iexact=nested") \
    || fail "carol could not list documents"
jq -e '.count == 0' <<<"$SEEN" >/dev/null || fail "carol sees nested.txt of dave"

echo "== asserting deprovisioning =="
api -X PATCH "$API/api/users/$USER_ID/" -d '{"groups":[]}' >/dev/null
wait_for "samba account not removed" lacks_account carol
lacks_account carol || fail "samba account not removed"
if compose exec -T smb test -e /shares/inbox/carol; then fail "folder not removed"; fi
WORKFLOWS=$(api "$API/api/workflows/" | jq -r '.results[].name') || fail "could not list workflows"
if grep -qx '\[smb-sync\] carol' <<<"$WORKFLOWS"; then fail "workflow not removed"; fi

echo "SMOKE TEST PASSED"
