from typing import Any

import requests

from .model import ManagedWorkflow, PaperlessUser

WORKFLOW_PREFIX = "[smb-sync] "

# verified against paperless-ngx source: WorkflowTriggerType.CONSUMPTION,
# DocumentSource.ConsumeFolder, WorkflowActionType.ASSIGNMENT
_TRIGGER_CONSUMPTION = 1
_SOURCE_CONSUME_FOLDER = 1
_ACTION_ASSIGNMENT = 1

_TIMEOUT = 30


class PaperlessError(Exception):
    pass


def _workflow_payload(user_id: int, username: str) -> dict[str, Any]:
    return {
        "name": f"{WORKFLOW_PREFIX}{username}",
        "order": 0,
        "enabled": True,
        "triggers": [
            {
                "type": _TRIGGER_CONSUMPTION,
                "sources": [_SOURCE_CONSUME_FOLDER],
                "filter_path": f"*/{username}/*",
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


class PaperlessClient:
    def __init__(
        self, base_url: str, token: str, session: requests.Session | None = None
    ):
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._session.headers["Authorization"] = f"Token {token}"

    def _get_all(self, path: str) -> list[dict[str, Any]]:
        url: str | None = f"{self._base}{path}"
        results: list[dict[str, Any]] = []
        while url:
            try:
                response = self._session.get(url, timeout=_TIMEOUT)
                response.raise_for_status()
            except requests.RequestException as exc:
                raise PaperlessError(f"GET {url} failed: {exc}") from exc
            payload = response.json()
            results.extend(payload["results"])
            url = payload.get("next")
        return results

    def fetch_desired_users(self, group_name: str) -> list[PaperlessUser]:
        groups = self._get_all("/api/groups/")
        group = next((g for g in groups if g["name"] == group_name), None)
        if group is None:
            raise PaperlessError(f"paperless group '{group_name}' does not exist")
        users = self._get_all("/api/users/")
        return [
            PaperlessUser(id=u["id"], username=u["username"], is_active=u["is_active"])
            for u in users
            if group["id"] in u["groups"]
        ]

    def fetch_managed_workflows(self) -> list[ManagedWorkflow]:
        workflows = self._get_all("/api/workflows/")
        return [
            ManagedWorkflow(id=w["id"], username=w["name"][len(WORKFLOW_PREFIX) :])
            for w in workflows
            if w["name"].startswith(WORKFLOW_PREFIX)
        ]

    def create_workflow(self, user_id: int, username: str) -> None:
        url = f"{self._base}/api/workflows/"
        try:
            response = self._session.post(
                url, json=_workflow_payload(user_id, username), timeout=_TIMEOUT
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise PaperlessError(f"POST {url} failed: {exc}") from exc

    def delete_workflow(self, workflow_id: int) -> None:
        url = f"{self._base}/api/workflows/{workflow_id}/"
        try:
            response = self._session.delete(url, timeout=_TIMEOUT)
            # a workflow already removed by hand is converged, not an error
            if response.status_code != 404:
                response.raise_for_status()
        except requests.RequestException as exc:
            raise PaperlessError(f"DELETE {url} failed: {exc}") from exc

    def rename_workflow(
        self, workflow_id: int, user_id: int, new_username: str
    ) -> None:
        url = f"{self._base}/api/workflows/{workflow_id}/"
        try:
            response = self._session.put(
                url, json=_workflow_payload(user_id, new_username), timeout=_TIMEOUT
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise PaperlessError(f"PUT {url} failed: {exc}") from exc
