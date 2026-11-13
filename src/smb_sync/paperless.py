import re
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import requests

from .model import (
    CredentialDocument,
    ManagedWorkflow,
    PaperlessUser,
    TokenUser,
    workflow_filter,
)

DOCUMENT_PREFIX = "[smb-sync] credentials "
WORKFLOW_PREFIX = "[smb-sync] "

_CREDENTIALS_FILE = "smb-credentials.txt"

# Until the hand-over the title carries the id of the user, since a rename can
# come between the upload and the end of its background import.
_DOCUMENT_TITLE = re.compile(
    re.escape(DOCUMENT_PREFIX)
    + r"(?P<username>[^ ]+)(?: \(user (?P<user_id>[0-9]+)\))?"
)


def _document_title(username: str, user_id: int | None = None) -> str:
    if user_id is None:
        return f"{DOCUMENT_PREFIX}{username}"
    return f"{DOCUMENT_PREFIX}{username} (user {user_id})"


# verified against paperless-ngx source: WorkflowTriggerType.CONSUMPTION,
# DocumentSource.ConsumeFolder, WorkflowActionType.ASSIGNMENT
_TRIGGER_CONSUMPTION = 1
_SOURCE_CONSUME_FOLDER = 1
_ACTION_ASSIGNMENT = 1

_TIMEOUT = 30


class PaperlessError(Exception):
    pass


@contextmanager
def _unexpected_body(what: str) -> Generator[None, None, None]:
    try:
        yield
    except (ValueError, KeyError, TypeError) as exc:
        raise PaperlessError(f"{what} returned an unexpected body: {exc}") from exc


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _join_date(raw: str) -> int:
    # cut to milliseconds and never rounded, as the export of paperless cuts a
    # timestamp, so that an entry still fits its user after an export and import
    delta = datetime.fromisoformat(raw) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _workflow_payload(user_id: int, username: str, consume_path: str) -> dict[str, Any]:
    return {
        "name": f"{WORKFLOW_PREFIX}{username}",
        "order": 0,
        "enabled": True,
        "triggers": [
            {
                "type": _TRIGGER_CONSUMPTION,
                "sources": [_SOURCE_CONSUME_FOLDER],
                "filter_path": workflow_filter(consume_path, username),
            }
        ],
        "actions": [
            {
                "type": _ACTION_ASSIGNMENT,
                "assign_owner": user_id,
                "assign_view_users": [user_id],
                "assign_change_users": [user_id],
            }
        ],
    }


def _trigger_filter(workflow: dict[str, Any]) -> str:
    # A workflow without exactly one trigger reads as no filter, so the planner
    # sees a difference and the update puts back the one trigger of the daemon.
    triggers = workflow["triggers"]
    if len(triggers) != 1:
        return ""
    return triggers[0]["filter_path"] or ""


def _credential_document(record: dict[str, Any]) -> CredentialDocument | None:
    # Each user who can change a document can give it such a title, and the
    # daemon deletes with the token of a superuser. The API cannot change the
    # file name of an upload, so the file name marks an upload of the daemon.
    match = _DOCUMENT_TITLE.fullmatch(record["title"])
    if match is None or record["original_file_name"] != _CREDENTIALS_FILE:
        return None
    user_id = match["user_id"]
    return CredentialDocument(
        id=record["id"],
        username=match["username"],
        owner_id=record["owner"],
        recipient_id=None if user_id is None else int(user_id),
    )


def _of_user(document: CredentialDocument, username: str, user_id: int) -> bool:
    # The name counts only for a document without an owner: paperless clears
    # the owner when the user is deleted, and the title is then all that links
    # the document to the account. A title can carry an earlier name of the
    # user, or a name that another user has now.
    return (
        document.owner_id == user_id
        or document.recipient_id == user_id
        or (document.owner_id is None and document.username == username)
    )


def _block_file(block: str) -> dict[str, tuple[str, bytes, str]]:
    return {"document": (_CREDENTIALS_FILE, block.encode("utf-8"), "text/plain")}


class PaperlessClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        consume_path: str,
        session: requests.Session | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._consume_path = consume_path
        self._session = session or requests.Session()
        self._session.headers["Authorization"] = f"Token {token}"

    def _send(
        self,
        method: str,
        path: str,
        *,
        tolerate: tuple[int, ...] = (),
        **kwargs: Any,
    ) -> requests.Response:
        # the next link of a listing page comes back as an absolute URL
        if path.startswith(("http://", "https://")):
            url = path
        else:
            url = f"{self._base}{path}"
        # requests repeats a redirected POST as a GET, so a write would report
        # success and never happen
        follow = method == "GET"
        try:
            response = self._session.request(
                method, url, timeout=_TIMEOUT, allow_redirects=follow, **kwargs
            )
            if response.status_code not in tolerate:
                response.raise_for_status()
        except requests.RequestException as exc:
            raise PaperlessError(f"{method} {url} failed: {exc}") from exc
        if not follow and 300 <= response.status_code < 400:
            raise PaperlessError(
                f"{method} {url} was redirected to "
                f"{response.headers.get('Location')}. Set PAPERLESS_URL to the "
                f"address that paperless answers on."
            )
        return response

    def _get_all(
        self, path: str, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        url: str | None = f"{self._base}{path}"
        # the next link already carries the query of the first request
        query: dict[str, str] | None = {"page_size": "1000", **(params or {})}
        results: list[dict[str, Any]] = []
        while url:
            response = self._send("GET", url, params=query)
            query = None
            with _unexpected_body(f"GET {url}"):
                payload = response.json()
                results.extend(payload["results"])
                url = payload.get("next")
        return results

    def _empty_trash(self, document_ids: Sequence[int]) -> None:
        # the action without ids empties the whole trash, so an empty list is
        # not sent either
        if document_ids:
            self._send(
                "POST",
                "/api/trash/",
                json={"action": "empty", "documents": document_ids},
            )

    def _trash_document(self, document_id: int) -> None:
        self._send("DELETE", f"/api/documents/{document_id}/", tolerate=(404,))

    def fetch_users(self, group_name: str) -> list[PaperlessUser]:
        groups = self._get_all("/api/groups/", {"name__iexact": group_name})
        # the server filter ignores case, so "SMB" comes back for "smb"
        group_id = next((g["id"] for g in groups if g["name"] == group_name), None)
        if group_id is None:
            raise PaperlessError(f"paperless group '{group_name}' does not exist")
        path = "/api/users/"
        users = self._get_all(path)
        with _unexpected_body(path):
            paperless_users = [
                PaperlessUser(
                    id=u["id"],
                    username=u["username"],
                    is_active=u["is_active"],
                    joined=_join_date(u["date_joined"]),
                    member=group_id in u["groups"],
                )
                for u in users
            ]
        return paperless_users

    def fetch_managed_workflows(self) -> list[ManagedWorkflow]:
        path = "/api/workflows/"
        workflows = self._get_all(path)
        with _unexpected_body(path):
            managed = [
                ManagedWorkflow(
                    id=w["id"],
                    username=w["name"].removeprefix(WORKFLOW_PREFIX),
                    filter_path=_trigger_filter(w),
                )
                for w in workflows
                if w["name"].startswith(WORKFLOW_PREFIX)
            ]
        return managed

    def create_workflow(self, user_id: int, username: str) -> None:
        self._send(
            "POST",
            "/api/workflows/",
            json=_workflow_payload(user_id, username, self._consume_path),
        )

    def delete_workflow(self, workflow_id: int) -> None:
        # a workflow already removed by hand is converged, not an error
        self._send("DELETE", f"/api/workflows/{workflow_id}/", tolerate=(404,))

    def update_workflow(self, workflow_id: int, user_id: int, username: str) -> None:
        self._send(
            "PUT",
            f"/api/workflows/{workflow_id}/",
            json=_workflow_payload(user_id, username, self._consume_path),
        )

    def add_credentials_version(self, document_id: int, block: str) -> None:
        self._send(
            "POST",
            f"/api/documents/{document_id}/update_version/",
            files=_block_file(block),
        )

    def ensure_document_note(self, document_id: int, note: str) -> None:
        # A retry finds the note of an earlier attempt and adds no second one.
        # paperless answers a failed read with 200 and an error object, which
        # fails the iteration below as an unexpected body.
        path = f"/api/documents/{document_id}/notes/"
        response = self._send("GET", path)
        with _unexpected_body(f"GET {path}"):
            present = any(n["note"] == note for n in response.json())
        if present:
            return
        response = self._send("POST", path, json={"note": note})
        # paperless also answers a note that it could not save with 200 and an
        # error object, and a title set after a lost note ends the retries. A
        # saved note comes back in the list of the notes of the document.
        with _unexpected_body(f"POST {path}"):
            notes = response.json()
            saved = isinstance(notes, list) and any(n["note"] == note for n in notes)
        if not saved:
            raise PaperlessError(
                f"paperless did not save the note on document {document_id}"
            )

    def delete_credentials(
        self, username: str, user_id: int, document_ids: Sequence[int]
    ) -> None:
        for document_id in document_ids:
            self._trash_document(document_id)
        # the listing also finds what a removal that stopped after its DELETE
        # left in the trash
        path = "/api/trash/"
        records = self._get_all(path)
        with _unexpected_body(path):
            documents = [(r["id"], _credential_document(r)) for r in records]
        # this call moved the selection to the trash, so its records count
        # whatever they show now
        removed = set(document_ids)
        trashed = [
            i
            for i, d in documents
            if i in removed or (d is not None and _of_user(d, username, user_id))
        ]
        self._empty_trash(trashed)

    def delete_document(self, document_id: int) -> None:
        # a document in the trash answers 404 and still needs the empty action
        self._trash_document(document_id)
        self._empty_trash([document_id])

    def fetch_credential_documents(self) -> list[CredentialDocument]:
        path = "/api/documents/"
        # The server filter ignores case, and a server that does not know it
        # returns every document, so the title is compared here. The text of a
        # document holds a valid password, so it stays out of the listing.
        records = self._get_all(
            path,
            {
                "title__istartswith": DOCUMENT_PREFIX,
                "fields": "id,original_file_name,owner,title",
            },
        )
        with _unexpected_body(path):
            documents = [_credential_document(r) for r in records]
        return [d for d in documents if d is not None]

    def hand_over_document(self, document_id: int, user_id: int, username: str) -> None:
        rights = {"users": [user_id], "groups": []}
        self._send(
            "PATCH",
            f"/api/documents/{document_id}/",
            json={
                "title": _document_title(username),
                "owner": user_id,
                "set_permissions": {"view": rights, "change": rights},
            },
        )

    def retitle_document(self, document_id: int, username: str) -> None:
        self._send(
            "PATCH",
            f"/api/documents/{document_id}/",
            json={"title": _document_title(username)},
        )

    def token_user(self) -> TokenUser:
        path = "/api/ui_settings/"
        response = self._send("GET", path)
        with _unexpected_body(path):
            user = response.json()["user"]
            token_user = TokenUser(id=user["id"], is_superuser=user["is_superuser"])
        return token_user

    def upload_credentials(self, user_id: int, username: str, block: str) -> None:
        self._send(
            "POST",
            "/api/documents/post_document/",
            data={"title": _document_title(username, user_id)},
            files=_block_file(block),
        )
