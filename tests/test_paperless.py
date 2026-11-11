import pytest

from smb_sync.model import ManagedWorkflow, PaperlessUser
from smb_sync.paperless import PaperlessClient, PaperlessError


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status {self.status_code}")


class BrokenJsonResponse(FakeResponse):
    def __init__(self):
        super().__init__(None)

    def json(self):
        raise ValueError("invalid body")


class FakeSession:
    """Route by exact URL; records every write request."""

    def __init__(self, routes, delete_status=204):
        self.routes = routes
        self.delete_status = delete_status
        self.headers = {}
        self.requests = []

    def get(self, url, timeout=None):
        return self.routes[url]

    def post(self, url, json=None, timeout=None):
        self.requests.append(("POST", url, json))
        return FakeResponse({}, 201)

    def put(self, url, json=None, timeout=None):
        self.requests.append(("PUT", url, json))
        return FakeResponse({}, 200)

    def delete(self, url, timeout=None):
        self.requests.append(("DELETE", url, None))
        return FakeResponse({}, self.delete_status)


BASE = "http://paperless:8000"


def page(results, next_url=None):
    return FakeResponse({"results": results, "next": next_url})


def make_client(routes):
    session = FakeSession(routes)
    return PaperlessClient(BASE, "tok", session=session), session


def test_fetch_desired_users_filters_by_group():
    client, _ = make_client(
        {
            f"{BASE}/api/groups/": page([{"id": 3, "name": "smb"}]),
            f"{BASE}/api/users/": page(
                [
                    {"id": 5, "username": "alice", "is_active": True, "groups": [3]},
                    {"id": 6, "username": "bob", "is_active": False, "groups": [3, 4]},
                    {"id": 7, "username": "carol", "is_active": True, "groups": []},
                ]
            ),
        }
    )
    users = client.fetch_desired_users("smb")
    assert users == [
        PaperlessUser(5, "alice", True),
        PaperlessUser(6, "bob", False),
    ]


def test_fetch_desired_users_carries_the_email_address():
    client, _ = make_client(
        {
            f"{BASE}/api/groups/": page([{"id": 3, "name": "smb"}]),
            f"{BASE}/api/users/": page(
                [
                    {
                        "id": 5,
                        "username": "alice",
                        "is_active": True,
                        "groups": [3],
                        "email": "alice@example.com",
                    },
                    {"id": 6, "username": "bob", "is_active": True, "groups": [3]},
                ]
            ),
        }
    )
    users = client.fetch_desired_users("smb")
    assert users[0].email == "alice@example.com"
    # paperless omits the field for users who never set one
    assert users[1].email == ""


def test_pagination_followed():
    client, _ = make_client(
        {
            f"{BASE}/api/groups/": page(
                [{"id": 1, "name": "other"}], next_url=f"{BASE}/api/groups/?page=2"
            ),
            f"{BASE}/api/groups/?page=2": page([{"id": 3, "name": "smb"}]),
            f"{BASE}/api/users/": page(
                [{"id": 5, "username": "alice", "is_active": True, "groups": [3]}]
            ),
        }
    )
    users = client.fetch_desired_users("smb")
    assert len(users) == 1


def test_missing_group_raises():
    client, _ = make_client({f"{BASE}/api/groups/": page([])})
    with pytest.raises(PaperlessError, match="does not exist"):
        client.fetch_desired_users("smb")


def test_http_error_raises_paperless_error():
    client, _ = make_client({f"{BASE}/api/groups/": FakeResponse({}, 500)})
    with pytest.raises(PaperlessError):
        client.fetch_desired_users("smb")


def test_non_json_body_raises_paperless_error():
    client, _ = make_client({f"{BASE}/api/groups/": BrokenJsonResponse()})
    with pytest.raises(PaperlessError, match="unexpected body"):
        client.fetch_desired_users("smb")


def test_missing_results_key_raises_paperless_error():
    client, _ = make_client({f"{BASE}/api/groups/": FakeResponse({"next": None})})
    with pytest.raises(PaperlessError, match="unexpected body"):
        client.fetch_desired_users("smb")


def test_fetch_managed_workflows_filters_prefix():
    client, _ = make_client(
        {
            f"{BASE}/api/workflows/": page(
                [
                    {"id": 7, "name": "[smb-sync] alice"},
                    {"id": 8, "name": "my manual workflow"},
                ]
            )
        }
    )
    assert client.fetch_managed_workflows() == [ManagedWorkflow(7, "alice")]


def test_create_workflow_payload():
    client, session = make_client({})
    client.create_workflow(5, "alice")
    method, url, payload = session.requests[0]
    assert (method, url) == ("POST", f"{BASE}/api/workflows/")
    assert payload["name"] == "[smb-sync] alice"
    trigger = payload["triggers"][0]
    assert trigger["type"] == 1
    assert trigger["sources"] == [1]
    assert trigger["filter_path"] == "*/alice/*"
    action = payload["actions"][0]
    assert action["type"] == 1
    assert action["assign_owner"] == 5
    assert action["assign_view_users"] == [5]
    assert action["assign_change_users"] == [5]


def test_delete_workflow_tolerates_404():
    session = FakeSession({}, delete_status=404)
    client = PaperlessClient(BASE, "tok", session=session)
    client.delete_workflow(7)
    assert session.requests == [("DELETE", f"{BASE}/api/workflows/7/", None)]


def test_rename_workflow_puts_full_payload():
    client, session = make_client({})
    client.rename_workflow(7, 5, "dave")
    method, url, payload = session.requests[0]
    assert (method, url) == ("PUT", f"{BASE}/api/workflows/7/")
    assert payload["name"] == "[smb-sync] dave"
    assert payload["triggers"][0]["filter_path"] == "*/dave/*"
    assert payload["actions"][0]["assign_owner"] == 5
