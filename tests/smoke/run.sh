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

# the smoke compose file keeps the default UID_BASE of 20000
account_of() { echo "u$((20000 + $1))"; }
cycle() { compose exec -T smb smb-sync sync; }
# The output of a cycle that the script starts reaches no container log, so
# cleanup prints the output of the last one when a run fails.
quiet_cycle() { LAST_CYCLE=$(cycle 2>&1) || fail "the sync command failed"; }
regen() { compose exec -T smb smb-sync regen "$1"; }
password_in() { awk '/Password:/{print $NF}' <<<"$1"; }

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
logged_line() {
    local logs
    logs=$(compose logs smb) || return
    grep -qF -- "$1" <<<"$logs"
}
# a log that cannot be read must not pass for a log without the text
log_lacks() {
    local logs
    logs=$(compose logs smb) || return
    ! grep -qF -- "$1" <<<"$logs"
}
map_has() { compose exec -T smb grep -qxF -- "$1" /var/lib/samba/smb-sync-usermap; }
smb_login() {
    # shellcheck disable=SC2016 # the variables expand in the shell of the container
    compose exec -T -e SMBLOGIN="$1" -e SMBPASS="$2" smb \
        sh -c 'smbclient //localhost/inbox -U "$SMBLOGIN%$SMBPASS" -c ls' >/dev/null 2>&1
}
# $3 is the status that Samba must answer
login_refused() {
    local out
    # shellcheck disable=SC2016 # the variables expand in the shell of the container
    out=$(compose exec -T -e SMBLOGIN="$1" -e SMBPASS="$2" smb \
        sh -c 'smbclient //localhost/inbox -U "$SMBLOGIN%$SMBPASS" -c ls' 2>&1) && return 1
    grep -q "$3" <<<"$out"
}
# writes a small file into the share under the name $3
smb_put() {
    # shellcheck disable=SC2016 # the variables expand in the shell of the container
    compose exec -T -e SMBLOGIN="$1" -e SMBPASS="$2" -e TARGET="$3" smb sh -c \
        'echo smoke >/tmp/put.txt &&
        smbclient //localhost/inbox -U "$SMBLOGIN%$SMBPASS" -c "put /tmp/put.txt $TARGET"' >/dev/null 2>&1
}
smb_lists() {
    local listing
    # shellcheck disable=SC2016 # the variables expand in the shell of the container
    listing=$(compose exec -T -e SMBLOGIN="$1" -e SMBPASS="$2" smb \
        sh -c 'smbclient //localhost/inbox -U "$SMBLOGIN%$SMBPASS" -c ls') || return
    grep -q "$3" <<<"$listing"
}
sessions_of() {
    local status
    status=$(compose exec -T smb smbstatus -b --json 2>/dev/null) || return
    jq -r --arg account "$1" '[.sessions[] | select(.username == $account)] | length' <<<"$status"
}
has_session() {
    local count
    count=$(sessions_of "$1") || return
    [ "$count" -ge 1 ]
}
no_session() {
    local count
    count=$(sessions_of "$1") || return
    [ "$count" = 0 ]
}
# The session outlasts a whole wait_for, so within a wait only the stop of the
# daemon can end it.
open_session() {
    # shellcheck disable=SC2016 # the variables expand in the shell of the container
    compose exec -d -e SMBLOGIN="$1" -e SMBPASS="$2" smb sh -c \
        '(echo ls; sleep 600) | smbclient //localhost/inbox -U "$SMBLOGIN%$SMBPASS" >/dev/null 2>&1'
}
entry_closed() {
    local entry
    entry=$(compose exec -T smb pdbedit -L -v -u "$1" 2>/dev/null) || return
    grep -Eq '^Account Flags: +\[[A-Z ]*D' <<<"$entry"
}
# prints the field $2 of the managed workflow of the account $1
workflow_field() {
    local workflows
    workflows=$(api "$API/api/workflows/") || return
    jq -r --arg name "[smb-sync] $1" --arg field "$2" \
        '.results[] | select(.name == $name) | .[$field]' <<<"$workflows"
}
first_cycle_done() { compose exec -T smb test -e /run/smb-sync.heartbeat; }
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
only_note_names() {
    local notes
    notes=$(api "$API/api/documents/$1/notes/") || return
    jq -e --arg name "$2" 'length == 1 and (.[0].note | contains($name))' \
        <<<"$notes" >/dev/null
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
# $1 names the case, $2 is a part of the log that gives the reason, the rest are arguments of docker run
refuses_to_start() {
    local what=$1 reason=$2 code logs
    shift 2
    docker rm -f -v smb-sync-smoke-check >/dev/null 2>&1 || true
    docker run -d --name smb-sync-smoke-check "$@" "$SMOKE_IMAGE" >/dev/null \
        || fail "could not start the container with $what"
    code=$(timeout 60 docker wait smb-sync-smoke-check) || true
    if [ -z "$code" ] || [ "$code" = 0 ]; then
        fail "the container with $what did not stop with an error"
    fi
    logs=$(docker logs smb-sync-smoke-check 2>&1) \
        || fail "could not read the log of the container with $what"
    grep -qF -- "$reason" <<<"$logs" || fail "the container with $what stopped for another reason"
}

cleanup() {
    local code=$?
    if [ "$code" != 0 ] && [ -n "${LAST_CYCLE+set}" ]; then
        echo "== the output of the last cycle that the script started ==" >&2
        printf '%s\n' "$LAST_CYCLE" >&2
    fi
    # the teardown below removes the container together with its log
    if [ "$code" != 0 ] && [ -n "$(compose ps -a -q smb 2>/dev/null)" ]; then
        echo "== the last lines of the log of the smb container ==" >&2
        compose logs --tail 40 smb >&2 || true
    fi
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    docker rm -f -v smb-sync-smoke-check >/dev/null 2>&1 || true
    docker volume rm smb-sync-smoke-base >/dev/null 2>&1 || true
    docker volume rm smb-sync-smoke-old >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

if [ -z "${SMOKE_IMAGE:-}" ]; then
    echo "== building the image =="
    docker build -t paperless-smb-sync:smoke ../..
    export SMOKE_IMAGE=paperless-smb-sync:smoke
fi

echo "== asserting that a container without a token stops =="
refuses_to_start "no token" "PAPERLESS_API_TOKEN" -e PAPERLESS_URL=http://paperless:8000

echo "== asserting that a container with another UID_BASE stops =="
docker rm -f -v smb-sync-smoke-check >/dev/null 2>&1 || true
docker run -d --name smb-sync-smoke-check -e PAPERLESS_URL=http://paperless:8000 \
    -e PAPERLESS_API_TOKEN=unset -v smb-sync-smoke-base:/var/lib/samba "$SMOKE_IMAGE" >/dev/null \
    || fail "could not start the container with the default UID_BASE"
# the volume check writes the map after the stored UID_BASE
wait_for "the map of the first start lacks the line '!nobody = *'" \
    docker exec smb-sync-smoke-check grep -sqxF -- '!nobody = *' /var/lib/samba/smb-sync-usermap
docker exec smb-sync-smoke-check test -e /var/lib/samba/smb-sync-uid-base \
    || fail "the first start did not store the UID_BASE"
refuses_to_start "another UID_BASE" "UID_BASE is 30000" -e PAPERLESS_URL=http://paperless:8000 \
    -e PAPERLESS_API_TOKEN=unset -e UID_BASE=30000 -v smb-sync-smoke-base:/var/lib/samba

echo "== asserting that a container on a volume of an earlier version stops =="
docker run --rm --entrypoint sh -v smb-sync-smoke-old:/var/lib/samba "$SMOKE_IMAGE" -c \
    'useradd --no-create-home alice && printf "pw-123456\npw-123456\n" | smbpasswd -s -a alice >/dev/null &&
    pdbedit -r -u alice -N "smb-sync uid=20005" >/dev/null' \
    || fail "could not make the entry of an earlier version"
refuses_to_start "a volume of an earlier version" "needs an empty Samba volume" \
    -e PAPERLESS_URL=http://paperless:8000 -e PAPERLESS_API_TOKEN=unset \
    -v smb-sync-smoke-old:/var/lib/samba

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
CAROL=$(account_of "$USER_ID")

echo "== starting smb container =="
compose up -d smb
wait_for "samba account of carol did not appear" has_account "$CAROL"
# the cycle after the creation makes the folder
wait_for "inbox folder of carol did not appear" \
    compose exec -T smb test -d "/shares/inbox/$CAROL"

echo "== asserting the creation =="
FOLDER=$(compose exec -T smb stat -c '%a %u %g' "/shares/inbox/$CAROL") \
    || fail "could not read the inbox folder of carol"
[ "$FOLDER" = "2770 ${CAROL#u} 10000" ] \
    || fail "the inbox folder of carol has the mode, owner and group $FOLDER"
has_workflow "$CAROL" || fail "workflow of carol missing"
CAROL_GROUPS=$(compose exec -T smb id -Gn "$CAROL") || fail "could not read the groups of carol"
[ "$CAROL_GROUPS" = smb-sync ] || fail "the account of carol is in the groups $CAROL_GROUPS"

echo "== asserting the first password =="
REGEN=$(regen carol) || fail "regen of carol failed"
PASSWORD=$(password_in "$REGEN")
[ -n "$PASSWORD" ] || fail "regen of carol printed no password"
# the login opens in the cycle that finds the workflow
wait_for "carol could not log in with the password of the regen" smb_login carol "$PASSWORD"

echo "== asserting the login name and the account name =="
smb_put carol "$PASSWORD" marker.keep || fail "carol could not write marker.keep"
# paperless takes no file with this ending, so the file stays for the steps below
compose exec -T smb test -f "/shares/inbox/$CAROL/marker.keep" \
    || fail "marker.keep is not in the inbox folder of carol"
login_refused "$CAROL" "$PASSWORD" NT_STATUS_LOGON_FAILURE \
    || fail "a login under the account name of carol was not refused"

echo "== asserting a scan =="
smb_put carol "$PASSWORD" smoke.txt || fail "carol could not write smoke.txt"
wait_for "smoke.txt was not imported as a document of carol" one_document_owned smoke "$USER_ID"

echo "== asserting a second password =="
REGEN=$(regen carol) || fail "the second regen of carol failed"
NEW=$(password_in "$REGEN")
[ -n "$NEW" ] || fail "the second regen of carol printed no password"
smb_login carol "$NEW" || fail "carol could not log in with the new password"
login_refused carol "$PASSWORD" NT_STATUS_LOGON_FAILURE \
    || fail "the old password of carol was not refused"
PASSWORD=$NEW

echo "== asserting a rename =="
api -X PATCH "$API/api/users/$USER_ID/" -d '{"username":"Carol.New"}' >/dev/null
wait_for "the map did not get the login name Carol.New" map_has "!$CAROL = Carol.New"
smb_login Carol.New "$PASSWORD" || fail "Carol.New could not log in"
login_refused carol "$PASSWORD" NT_STATUS_LOGON_FAILURE \
    || fail "the old login name carol was not refused"
smb_lists Carol.New "$PASSWORD" marker.keep || fail "Carol.New does not see marker.keep"
has_account "$CAROL" || fail "the account of carol is gone after the rename"

echo "== asserting the owner of a scan in a subfolder =="
DAVE_ID=$(api -X POST "$API/api/users/" \
    -d "{\"username\":\"dave\",\"password\":\"paperless-pass\",\"groups\":[$GROUP_ID]}" | jq -r .id)
DAVE=$(account_of "$DAVE_ID")
wait_for "inbox folder of dave did not appear" compose exec -T smb test -d "/shares/inbox/$DAVE"
# a scan that reaches paperless before the workflow exists gets no owner
wait_for "workflow of dave did not appear" has_workflow "$DAVE"
REGEN=$(regen dave) || fail "regen of dave failed"
DAVE_PASSWORD=$(password_in "$REGEN")
[ -n "$DAVE_PASSWORD" ] || fail "regen of dave printed no password"
# paperless misses a file written before its watcher has caught up with a new
# folder, so the folder is made in a call of its own
compose exec -T smb mkdir -m 2770 "/shares/inbox/$DAVE/$CAROL" \
    || fail "could not make the subfolder"
compose exec -T smb sh -c "echo 'own scan' >/shares/inbox/$DAVE/own.txt &&
    echo 'nested scan' >/shares/inbox/$DAVE/$CAROL/nested.txt" \
    || fail "could not write the scans of dave"
wait_for "nested.txt was not imported" one_document nested
wait_for "own.txt was not imported" one_document own
owned_alone own "$DAVE_ID" || fail "own.txt is not owned by dave alone"
owned_alone nested "$DAVE_ID" || fail "nested.txt is not owned by dave alone"
SEEN=$(as_user dave paperless-pass "$API/api/documents/?title__iexact=nested") \
    || fail "dave could not list documents"
jq -e '.count == 1' <<<"$SEEN" >/dev/null || fail "dave does not see nested.txt"
SEEN=$(as_user Carol.New paperless-pass "$API/api/documents/?title__iexact=nested") \
    || fail "Carol.New could not list documents"
jq -e '.count == 0' <<<"$SEEN" >/dev/null || fail "Carol.New sees nested.txt of dave"

echo "== asserting that no credentials document appears with the option off =="
SEEN=$(api -G --data-urlencode 'title__istartswith=[smb-sync] credentials' \
    "$API/api/documents/") || fail "could not list documents"
jq -e '.count == 0' <<<"$SEEN" >/dev/null \
    || fail "a credentials document appeared with the option off"

echo "== asserting a settled state =="
# the first cycle takes up what the daemon has not reached yet
quiet_cycle
quiet_cycle
SETTLED=$LAST_CYCLE
[ -z "$SETTLED" ] || fail "a cycle in a settled state did something: $SETTLED"

echo "== asserting deprovisioning =="
api -X PATCH "$API/api/users/$USER_ID/" -d '{"groups":[]}' >/dev/null
wait_for "samba account of carol not removed" lacks_account "$CAROL"
# the entry goes last, so each other part is gone by now
# getent answers 2 for a name that it does not find, so an exec that fails
# does not pass for a removed account
code=0
compose exec -T smb getent passwd "$CAROL" >/dev/null || code=$?
[ "$code" = 2 ] || fail "unix account of carol not removed"
compose exec -T smb test ! -e "/shares/inbox/$CAROL" || fail "folder of carol not removed"
WORKFLOWS=$(api "$API/api/workflows/" | jq -r '.results[].name') || fail "could not list workflows"
if grep -qxF "[smb-sync] $CAROL" <<<"$WORKFLOWS"; then fail "workflow of carol not removed"; fi
grep -qxF "[smb-sync] $DAVE" <<<"$WORKFLOWS" || fail "the removal of carol took the workflow of dave"
MAP=$(compose exec -T smb cat /var/lib/samba/smb-sync-usermap) || fail "could not read the map"
if grep -q "^!$CAROL " <<<"$MAP"; then fail "the map still has a line of carol"; fi
grep -q "^!$DAVE " <<<"$MAP" || fail "the removal of carol took the line of dave out of the map"

echo "== asserting a new account before its login opens =="
# Each state below lasts one cycle, which a daemon that runs every 5 seconds
# would end before a check. From here the daemon sleeps an hour after its first
# cycle, and the script starts each cycle.
# The first cycle of the new container makes the account of frank, so that its
# log is the one that the checks read. The old container would make it first,
# without the document, so it stops before frank exists.
compose stop smb
FRANK_ID=$(api -X POST "$API/api/users/" \
    -d "{\"username\":\"frank\",\"password\":\"paperless-pass\",\"groups\":[$GROUP_ID]}" | jq -r .id)
FRANK=$(account_of "$FRANK_ID")
export SMOKE_DOCUMENT=true SMOKE_POLL=3600
compose up -d smb
wait_for "the smb container did not finish its first cycle" first_cycle_done
UPLOAD="[smb-sync] credentials frank (user $FRANK_ID)"
wait_for "the credentials document of frank did not appear" sees_document "$TOKEN" "$UPLOAD"
DOCUMENT=$(with_token "$TOKEN" -G --data-urlencode "title__iexact=$UPLOAD" "$API/api/documents/") \
    || fail "could not list documents"
DOCUMENT_ID=$(jq -er '.results[0].id' <<<"$DOCUMENT") || fail "no id for the document of frank"
FRANK_PASSWORD=$(password_in "$(jq -r '.results[0].content' <<<"$DOCUMENT")")
[ -n "$FRANK_PASSWORD" ] || fail "the document of frank holds no password"
login_refused frank "$FRANK_PASSWORD" NT_STATUS_ACCOUNT_DISABLED \
    || fail "the login of frank was open before the cycle that finds the workflow"
log_lacks "$FRANK_PASSWORD" || fail "the log holds the password of frank"
# a search that finds nothing would pass the check above
logged_line "smb-sync regen frank" || fail "the log lacks the line of the creation of frank"
quiet_cycle
smb_login frank "$FRANK_PASSWORD" || fail "frank could not log in with the password of the document"

echo "== asserting the credentials document =="
# the cycle that opened the login also handed the document over
FRANK_TOKEN=$(user_token frank paperless-pass) || fail "could not obtain the token of frank"
sees_document "$FRANK_TOKEN" '[smb-sync] credentials frank' \
    || fail "frank does not see his credentials document"
document_holds "$FRANK_TOKEN" "$DOCUMENT_ID" "$FRANK_PASSWORD" \
    || fail "the document of frank does not hold his password"
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
REGEN=$(regen frank) || fail "regen of frank failed"
# the regen resets the rights before it returns, so this check does not wait
COUNT=$(titled_count "$DAVE_TOKEN" '[smb-sync] credentials frank') \
    || fail "dave could not list documents"
[ "$COUNT" = 0 ] || fail "dave still sees the document of frank after the regen"
FRANK_PASSWORD=$(password_in "$REGEN")
[ -n "$FRANK_PASSWORD" ] || fail "regen of frank printed no password"
# paperless imports the new version in the background
wait_for "the document of frank did not get the new password" \
    document_holds "$FRANK_TOKEN" "$DOCUMENT_ID" "$FRANK_PASSWORD"
api -X PATCH "$API/api/users/$FRANK_ID/" -d '{"username":"gina"}' >/dev/null \
    || fail "could not rename frank to gina"
quiet_cycle
document_titled "$DOCUMENT_ID" '[smb-sync] credentials gina' \
    || fail "the document was not retitled for gina"
only_note_names "$DOCUMENT_ID" gina \
    || fail "the document of gina does not have exactly one note that names gina"
smb_login gina "$FRANK_PASSWORD" || fail "gina could not log in with the password of the regen"
api -X PATCH "$API/api/users/$FRANK_ID/" -d '{"groups":[]}' >/dev/null \
    || fail "could not take gina out of the group"
quiet_cycle
document_gone "$DOCUMENT_ID" || fail "the document of gina was not deleted"
trash_empty || fail "the trash still holds a document"
lacks_account "$FRANK" || fail "samba account of gina not removed"

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
quiet_cycle
document_gone "$GHOST_ID" || fail "the credentials file of ghost was not deleted"
trash_empty || fail "the trash still holds a document"
api "$API/api/documents/$PHANTOM_ID/" >/dev/null \
    || fail "the notes of phantom were deleted for their title alone"

echo "== asserting a closed login while the workflow is gone =="
open_session dave "$DAVE_PASSWORD" || fail "could not open a session of dave"
wait_for "the session of dave did not appear" has_session "$DAVE"
WORKFLOW_ID=$(workflow_field "$DAVE" id) || fail "could not list workflows"
[ -n "$WORKFLOW_ID" ] || fail "workflow of dave missing"
api -X DELETE "$API/api/workflows/$WORKFLOW_ID/" >/dev/null \
    || fail "could not delete the workflow of dave"
quiet_cycle
wait_for "the session of dave did not end" no_session "$DAVE"
login_refused dave "$DAVE_PASSWORD" NT_STATUS_ACCOUNT_DISABLED \
    || fail "the login of dave stayed open without its workflow"
has_workflow "$DAVE" || fail "the workflow of dave was not made again"
quiet_cycle
smb_login dave "$DAVE_PASSWORD" || fail "dave could not log in once his workflow was back"

echo "== asserting a closed login while the workflow is disabled =="
WORKFLOW_ID=$(workflow_field "$DAVE" id) || fail "could not list workflows"
[ -n "$WORKFLOW_ID" ] || fail "workflow of dave missing"
api -X PATCH "$API/api/workflows/$WORKFLOW_ID/" -d '{"enabled":false}' >/dev/null \
    || fail "could not disable the workflow of dave"
quiet_cycle
login_refused dave "$DAVE_PASSWORD" NT_STATUS_ACCOUNT_DISABLED \
    || fail "the login of dave stayed open with a disabled workflow"
ENABLED=$(workflow_field "$DAVE" enabled) || fail "could not list workflows"
[ "$ENABLED" = true ] || fail "the workflow of dave was not enabled again"
quiet_cycle
smb_login dave "$DAVE_PASSWORD" || fail "dave could not log in once his workflow was enabled"

echo "== asserting a closed login while the user is inactive =="
open_session dave "$DAVE_PASSWORD" || fail "could not open a session of dave"
wait_for "the session of dave did not appear" has_session "$DAVE"
api -X PATCH "$API/api/users/$DAVE_ID/" -d '{"is_active":false}' >/dev/null \
    || fail "could not deactivate dave"
quiet_cycle
wait_for "the session of dave did not end" no_session "$DAVE"
login_refused dave "$DAVE_PASSWORD" NT_STATUS_ACCOUNT_DISABLED \
    || fail "the login of dave stayed open while he was inactive"
api -X PATCH "$API/api/users/$DAVE_ID/" -d '{"is_active":true}' >/dev/null \
    || fail "could not activate dave"
quiet_cycle
smb_login dave "$DAVE_PASSWORD" || fail "dave could not log in once he was active again"

echo "== asserting that a cycle ends a session left on a closed login =="
open_session dave "$DAVE_PASSWORD" || fail "could not open a session of dave"
wait_for "the session of dave did not appear" has_session "$DAVE"
# a closed entry with an open session is the state after a stop that failed
compose exec -T smb smbpasswd -d "$DAVE" >/dev/null || fail "could not close the entry of dave"
api -X PATCH "$API/api/users/$DAVE_ID/" -d '{"is_active":false}' >/dev/null \
    || fail "could not deactivate dave"
quiet_cycle
wait_for "the session of dave on a closed entry did not end" no_session "$DAVE"
entry_closed "$DAVE" || fail "the entry of dave did not stay closed"
api -X PATCH "$API/api/users/$DAVE_ID/" -d '{"is_active":true}' >/dev/null \
    || fail "could not activate dave"
quiet_cycle
smb_login dave "$DAVE_PASSWORD" || fail "dave could not log in once he was active again"

echo "== asserting the hold for an entry of another join date =="
ERIN_ID=$(api -X POST "$API/api/users/" \
    -d "{\"username\":\"erin\",\"password\":\"paperless-pass\",\"groups\":[$GROUP_ID]}" | jq -r .id)
ERIN=$(account_of "$ERIN_ID")
quiet_cycle
quiet_cycle
has_account "$ERIN" || fail "samba account of erin did not appear"
# so that a closed entry in the hold below is the work of the hold
if entry_closed "$ERIN"; then fail "the login of erin did not open before the hold"; fi
compose exec -T smb test -d "/shares/inbox/$ERIN" || fail "inbox folder of erin did not appear"
DAVE_RECORD=$(api "$API/api/users/$DAVE_ID/") || fail "could not read the user dave"
JOINED=$(jq -er .date_joined <<<"$DAVE_RECORD") || fail "dave has no join date"
api -X PATCH "$API/api/users/$DAVE_ID/" -d '{"date_joined":"2001-02-03T04:05:06Z"}' >/dev/null \
    || fail "could not change the join date of dave"
# a join date that paperless did not keep must not read as a defect of the hold
DAVE_RECORD=$(api "$API/api/users/$DAVE_ID/") || fail "could not read the user dave"
[[ $(jq -r .date_joined <<<"$DAVE_RECORD") == 2001-02-03T04:05:06* ]] \
    || fail "paperless did not take the new join date"
api -X PATCH "$API/api/users/$ERIN_ID/" -d '{"groups":[]}' >/dev/null \
    || fail "could not take erin out of the group"
quiet_cycle
entry_closed "$DAVE" || fail "the entry of dave stayed open with another join date"
# the map has no line for dave during the hold
login_refused dave "$DAVE_PASSWORD" NT_STATUS_LOGON_FAILURE \
    || fail "the login name dave was not refused during the hold"
has_account "$ERIN" || fail "the hold removed the samba account of erin"
entry_closed "$ERIN" || fail "the entry of erin stayed open during the hold"
compose exec -T smb test -d "/shares/inbox/$ERIN" || fail "the hold removed the folder of erin"
api -X PATCH "$API/api/users/$DAVE_ID/" -d "$(jq -n --arg joined "$JOINED" '{date_joined: $joined}')" \
    >/dev/null || fail "could not set the join date of dave back"
quiet_cycle
smb_login dave "$DAVE_PASSWORD" || fail "dave could not log in after the hold"
lacks_account "$ERIN" || fail "samba account of erin not removed after the hold"
compose exec -T smb test ! -e "/shares/inbox/$ERIN" || fail "folder of erin not removed after the hold"

echo "== asserting a login after a recreation without paperless =="
compose stop paperless
compose up -d --force-recreate --no-deps smb
wait_for "dave could not log in after the recreation" smb_login dave "$DAVE_PASSWORD"
# wait_for allows about two minutes, more than the start period of the health
# check plus one interval
wait_for "the smb container did not become healthy" smb_healthy

echo "SMOKE TEST PASSED"
