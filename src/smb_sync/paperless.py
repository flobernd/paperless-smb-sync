import re
from collections.abc import Sequence
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

# Until the hand-over the title carries the id of the user, since a rename can
# come between the upload and the end of its background import.
_DOCUMENT_TITLE = re.compile(
    re.escape(DOCUMENT_PREFIX)
    + r"(?P<username>[^ ]+)(?: \(user (?P<user_id>[0-9]+)\))?"
)

# verified against paperless-ngx source: WorkflowTriggerType.CONSUMPTION,
# DocumentSource.ConsumeFolder, WorkflowActionType.ASSIGNMENT
_TRIGGER_CONSUMPTION = 1
_SOURCE_CONSUME_FOLDER = 1
_ACTION_ASSIGNMENT = 1

_TIMEOUT = 30


class PaperlessError(Exception):
    pass


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
    match = _DOCUMENT_TITLE.fullmatch(record["title"])
    if match is None:
        return None
    user_id = match["user_id"]
    return CredentialDocument(
        id=record["id"],
        username=match["username"],
        owner_id=record["owner"],
        recipient_id=None if user_id is None else int(user_id),
    )


def _block_file(block: str) -> dict[str, tuple[str, bytes, str]]:
    return {"document": ("smb-credentials.txt", block.encode("utf-8"), "text/plain")}


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
        try:
            response = self._session.request(method, url, timeout=_TIMEOUT, **kwargs)
            if response.status_code not in tolerate:
                response.raise_for_status()
        except requests.RequestException as exc:
            raise PaperlessError(f"{method} {url} failed: {exc}") from exc
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
            try:
                payload = response.json()
                results.extend(payload["results"])
                url = payload.get("next")
            except (ValueError, KeyError, TypeError) as exc:
                raise PaperlessError(
                    f"GET {url} returned an unexpected body: {exc}"
                ) from exc
        return results

    def fetch_desired_users(self, group_name: str) -> list[PaperlessUser]:
        groups = self._get_all("/api/groups/", {"name__iexact": group_name})
        # the server filter ignores case, so "SMB" comes back for "smb"
        group_id = next((g["id"] for g in groups if g["name"] == group_name), None)
        if group_id is None:
            raise PaperlessError(f"paperless group '{group_name}' does not exist")
        path = "/api/users/"
        users = self._get_all(path)
        try:
            return [
                PaperlessUser(
                    id=u["id"],
                    username=u["username"],
                    is_active=u["is_active"],
                )
                for u in users
                if group_id in u["groups"]
            ]
        except (KeyError, TypeError) as exc:
            raise PaperlessError(f"{path} returned an unexpected body: {exc}") from exc

    def fetch_managed_workflows(self) -> list[ManagedWorkflow]:
        path = "/api/workflows/"
        workflows = self._get_all(path)
        try:
            return [
                ManagedWorkflow(
                    id=w["id"],
                    username=w["name"][len(WORKFLOW_PREFIX) :],
                    filter_path=_trigger_filter(w),
                )
                for w in workflows
                if w["name"].startswith(WORKFLOW_PREFIX)
            ]
        except (KeyError, TypeError) as exc:
            raise PaperlessError(f"{path} returned an unexpected body: {exc}") from exc

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

    def add_document_note(self, document_id: int, note: str) -> None:
        self._send(
            "POST", f"/api/documents/{document_id}/notes/", json={"note": note}
        )

    def delete_credentials(self, username: str, document_ids: Sequence[int]) -> None:
        for document_id in document_ids:
            self._send("DELETE", f"/api/documents/{document_id}/", tolerate=(404,))
        # the listing also finds what a removal that stopped after its DELETE
        # left in the trash
        path = "/api/trash/"
        records = self._get_all(path)
        try:
            documents = [(r["id"], _credential_document(r)) for r in records]
        except (KeyError, TypeError) as exc:
            raise PaperlessError(f"{path} returned an unexpected body: {exc}") from exc
        # a document of the removal can still carry an earlier name of the user
        removed = set(document_ids)
        trashed = [
            i
            for i, d in documents
            if i in removed or (d is not None and d.username == username)
        ]
        # the action without ids empties the whole trash, so an empty list is
        # not sent either
        if trashed:
            self._send(
                "POST", path, json={"action": "empty", "documents": trashed}
            )

    def delete_document(self, document_id: int) -> None:
        # a document in the trash answers 404 and still needs the empty action
        self._send("DELETE", f"/api/documents/{document_id}/", tolerate=(404,))
        self._send(
            "POST", "/api/trash/", json={"action": "empty", "documents": [document_id]}
        )

    def fetch_credential_documents(self) -> list[CredentialDocument]:
        path = "/api/documents/"
        # The server filter ignores case, and a server that does not know it
        # returns every document, so the title is compared here. The text of a
        # document holds a valid password, so it stays out of the listing.
        records = self._get_all(
            path, {"title__istartswith": DOCUMENT_PREFIX, "fields": "id,owner,title"}
        )
        try:
            documents = [_credential_document(r) for r in records]
        except (KeyError, TypeError) as exc:
            raise PaperlessError(f"{path} returned an unexpected body: {exc}") from exc
        return [d for d in documents if d is not None]

    def hand_over_document(self, document_id: int, user_id: int, username: str) -> None:
        rights = {"users": [user_id], "groups": []}
        self._send(
            "PATCH",
            f"/api/documents/{document_id}/",
            json={
                "title": f"{DOCUMENT_PREFIX}{username}",
                "owner": user_id,
                "set_permissions": {"view": rights, "change": rights},
            },
        )

    def retitle_document(self, document_id: int, username: str) -> None:
        self._send(
            "PATCH",
            f"/api/documents/{document_id}/",
            json={"title": f"{DOCUMENT_PREFIX}{username}"},
        )

    def token_user(self) -> TokenUser:
        path = "/api/ui_settings/"
        response = self._send("GET", path)
        try:
            user = response.json()["user"]
            return TokenUser(id=user["id"], is_superuser=user["is_superuser"])
        except (ValueError, KeyError, TypeError) as exc:
            raise PaperlessError(f"{path} returned an unexpected body: {exc}") from exc

    def upload_credentials(self, user_id: int, username: str, block: str) -> None:
        self._send(
            "POST",
            "/api/documents/post_document/",
            data={"title": f"{DOCUMENT_PREFIX}{username} (user {user_id})"},
            files=_block_file(block),
        )
