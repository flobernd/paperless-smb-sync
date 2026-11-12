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

user_token() {
    curl -fsS -X POST "$API/api/token/" -H 'Content-Type: application/json' \
        -d "$(jq -n --arg username "$1" --arg password "$2" '{$username, $password}')" \
        | jq -er .token
}
as_user() {
    local token
    token=$(user_token "$1" "$2") || return
    shift 2
    curl -fsS -H "Authorization: Token $token" "$@"
}
# paperless answers 429 to the sixth token request in a minute, so a check that
# wait_for repeats takes a token that the caller fetched once
with_token() {
    local token=$1
    shift
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
account_renamed() { has_account "$2" && lacks_account "$1"; }
logged_block() {
    local logs
    logs=$(compose logs smb) || return
    grep -q "SMB credentials for '$1'" <<<"$logs"
}
sees_document() {
    local documents
    documents=$(with_token "$1" -G --data-urlencode "title__iexact=$2" \
        "$API/api/documents/") || return
    jq -e --arg title "$2" '.count == 1 and .results[0].title == $title' \
        <<<"$documents" >/dev/null
}
titled_count() {
    local documents
    documents=$(with_token "$1" -G --data-urlencode "title__iexact=$2" \
        "$API/api/documents/") || return
    jq -er '.count' <<<"$documents"
}
document_holds() {
    local document
    document=$(with_token "$1" "$API/api/documents/$2/") || return
    jq -e --arg password "$3" '.content | contains($password)' <<<"$document" >/dev/null
}
document_titled() {
    local document
    document=$(api "$API/api/documents/$1/") || return
    jq -e --arg title "$2" '.title == $title' <<<"$document" >/dev/null
}
note_names() {
    local notes
    notes=$(api "$API/api/documents/$1/notes/") || return
    jq -e --arg name "$2" 'any(.[]; .note | contains($name))' <<<"$notes" >/dev/null
}
document_gone() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Token $TOKEN" \
        "$API/api/documents/$1/") || return
    [ "$code" = 404 ]
}
trash_empty() {
    local trash
    trash=$(api "$API/api/trash/") || return
    jq -e '.count == 0' <<<"$trash" >/dev/null
}
# the json content type of api would replace the multipart one of the upload
upload_text() {
    with_token "$TOKEN" -F "title=$1" -F "document=@-;filename=$2" \
        "$API/api/documents/post_document/" <<<"$3" >/dev/null
}
titled_id() {
    local documents
    documents=$(with_token "$TOKEN" -G --data-urlencode "title__iexact=$1" \
        "$API/api/documents/") || return
    jq -er '.results[0].id' <<<"$documents"
}
smb_healthy() {
    local status
    status=$(docker inspect --format '{{.State.Health.Status}}' "$(compose ps -q smb)") \
        || return
    [ "$status" = healthy ]
}

cleanup() {
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    docker rm -f -v smb-sync-smoke-check >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

if [ -z "${SMOKE_IMAGE:-}" ]; then
    echo "== building the image =="
    docker build -t paperless-smb-sync:smoke ../..
    export SMOKE_IMAGE=paperless-smb-sync:smoke
fi

echo "== asserting that a container without a token stops =="
docker run -d --name smb-sync-smoke-check -e PAPERLESS_URL=http://paperless:8000 \
    "$SMOKE_IMAGE" >/dev/null || fail "could not start the check container"
CODE=$(timeout 60 docker wait smb-sync-smoke-check) || true
if [ -z "$CODE" ] || [ "$CODE" = 0 ]; then
    fail "the container without a token did not stop with an error"
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

echo "== asserting the credentials document =="
SEEN=$(api -G --data-urlencode 'title__istartswith=[smb-sync] credentials carol' \
    "$API/api/documents/") || fail "could not list documents"
jq -e '.count == 0' <<<"$SEEN" >/dev/null \
    || fail "a credentials document of carol appeared with the option off"
export SMOKE_DOCUMENT=true
compose up -d smb
FRANK_ID=$(api -X POST "$API/api/users/" \
    -d "{\"username\":\"frank\",\"password\":\"paperless-pass\",\"groups\":[$GROUP_ID]}" | jq -r .id)
wait_for "credentials block of frank not found in logs" logged_block frank
FRANK_PASSWORD=$(compose logs smb | grep -A4 "SMB credentials for 'frank'" \
    | awk '/Password:/{print $NF}' | tail -1) || true
[ -n "$FRANK_PASSWORD" ] || fail "password of frank not found in logs"
FRANK_TOKEN=$(user_token frank paperless-pass) || fail "could not obtain the token of frank"
wait_for "frank does not see his credentials document" \
    sees_document "$FRANK_TOKEN" '[smb-sync] credentials frank'
DOCUMENT=$(with_token "$FRANK_TOKEN" -G \
    --data-urlencode 'title__iexact=[smb-sync] credentials frank' "$API/api/documents/") \
    || fail "frank could not list documents"
DOCUMENT_ID=$(jq -er '.results[0].id' <<<"$DOCUMENT") || fail "no id for the document of frank"
jq -e --arg password "$FRANK_PASSWORD" '.results[0].content | contains($password)' \
    <<<"$DOCUMENT" >/dev/null || fail "the document of frank does not hold his password"
DAVE_TOKEN=$(user_token dave paperless-pass) || fail "could not obtain the token of dave"
COUNT=$(titled_count "$DAVE_TOKEN" '[smb-sync] credentials frank') \
    || fail "dave could not list documents"
[ "$COUNT" = 0 ] || fail "dave sees the credentials document of frank"
api -X PATCH "$API/api/documents/$DOCUMENT_ID/" \
    -d "{\"set_permissions\":{\"view\":{\"users\":[$DAVE_ID],\"groups\":[]}}}" >/dev/null \
    || fail "could not give dave a view right on the document of frank"
COUNT=$(titled_count "$DAVE_TOKEN" '[smb-sync] credentials frank') \
    || fail "dave could not list documents"
[ "$COUNT" = 1 ] || fail "dave does not see the document of frank after the grant"
REGEN=$(compose exec -T smb smb-sync regen frank) || fail "regen of frank failed"
# the regen resets the rights before it returns, so this check does not wait
COUNT=$(titled_count "$DAVE_TOKEN" '[smb-sync] credentials frank') \
    || fail "dave could not list documents"
[ "$COUNT" = 0 ] || fail "dave still sees the document of frank after the regen"
NEW_PASSWORD=$(awk '/Password:/{print $NF}' <<<"$REGEN")
[ -n "$NEW_PASSWORD" ] || fail "regen of frank printed no password"
wait_for "the document of frank did not get the new password" \
    document_holds "$FRANK_TOKEN" "$DOCUMENT_ID" "$NEW_PASSWORD"
api -X PATCH "$API/api/users/$FRANK_ID/" -d '{"username":"gina"}' >/dev/null
wait_for "the document was not retitled for gina" \
    document_titled "$DOCUMENT_ID" '[smb-sync] credentials gina'
# the note follows the new title in a request of its own
wait_for "no note of the document names gina" note_names "$DOCUMENT_ID" gina
api -X PATCH "$API/api/users/$FRANK_ID/" -d '{"groups":[]}' >/dev/null
wait_for "the document of gina was not deleted" document_gone "$DOCUMENT_ID"
# the empty action follows the DELETE in a request of its own
wait_for "the trash still holds a document" trash_empty

echo "== asserting the deletion of a credentials document without an owner =="
upload_text '[smb-sync] credentials ghost' smb-credentials.txt 'smoke credentials of ghost' \
    || fail "could not upload the credentials file of ghost"
upload_text '[smb-sync] credentials phantom' notes.txt 'smoke notes of phantom' \
    || fail "could not upload the notes of phantom"
wait_for "the credentials file of ghost was not imported" \
    sees_document "$TOKEN" '[smb-sync] credentials ghost'
wait_for "the notes of phantom were not imported" \
    sees_document "$TOKEN" '[smb-sync] credentials phantom'
GHOST_ID=$(titled_id '[smb-sync] credentials ghost') || fail "no id for the document of ghost"
PHANTOM_ID=$(titled_id '[smb-sync] credentials phantom') \
    || fail "no id for the document of phantom"
for id in "$GHOST_ID" "$PHANTOM_ID"; do
    api -X PATCH "$API/api/documents/$id/" -d '{"owner":null}' >/dev/null \
        || fail "could not remove the owner of document $id"
done
wait_for "the credentials file of ghost was not deleted" document_gone "$GHOST_ID"
wait_for "the trash still holds a document" trash_empty
# A cycle can delete the first document and still be on its way to the
# second. The sync waits for the cycle lock, so the check below follows a
# whole cycle that saw both documents without an owner.
compose exec -T smb smb-sync sync >/dev/null || fail "the sync command failed"
api "$API/api/documents/$PHANTOM_ID/" >/dev/null \
    || fail "the notes of phantom were deleted for their title alone"

echo "== asserting deprovisioning =="
api -X PATCH "$API/api/users/$USER_ID/" -d '{"groups":[]}' >/dev/null
wait_for "samba account not removed" lacks_account carol
if compose exec -T smb test -e /shares/inbox/carol; then fail "folder not removed"; fi
WORKFLOWS=$(api "$API/api/workflows/" | jq -r '.results[].name') || fail "could not list workflows"
if grep -qx '\[smb-sync\] carol' <<<"$WORKFLOWS"; then fail "workflow not removed"; fi

echo "== asserting a login after a recreation without paperless =="
api -X PATCH "$API/api/users/$DAVE_ID/" -d '{"username":"erin"}' >/dev/null
wait_for "samba account of dave was not renamed to erin" account_renamed dave erin
compose stop paperless
compose up -d --force-recreate --no-deps smb
# shellcheck disable=SC2016 # $SMBPASS expands in the shell of the container
wait_for "erin could not log in after the recreation" \
    compose exec -T -e SMBPASS="$DAVE_PASSWORD" smb \
    sh -c 'smbclient //localhost/inbox -U "erin%$SMBPASS" -c ls'
# wait_for allows about two minutes, more than the start period of the health
# check plus one interval
wait_for "the smb container did not become healthy" smb_healthy

echo "SMOKE TEST PASSED"
