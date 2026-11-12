import pytest

from smb_sync.model import CredentialDocument, ManagedWorkflow, PaperlessUser
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

    def request(self, method, url, **kwargs):
        if method == "GET":
            return self.routes[url]
        self.requests.append((method, url, kwargs.get("json")))
        status = {"DELETE": self.delete_status, "POST": 201, "PUT": 200}[method]
        return FakeResponse({}, status)


BASE = "http://paperless:8000"
CONSUME = "/usr/src/paperless/consume"


def page(results, next_url=None):
    return FakeResponse({"results": results, "next": next_url})


def make_client(routes):
    session = FakeSession(routes)
    return PaperlessClient(BASE, "tok", CONSUME, session=session), session


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
    # the body parses and holds no user, so only the status check keeps a 500
    # from reading as "no users"
    client, _ = make_client(
        {
            f"{BASE}/api/groups/": page([{"id": 3, "name": "smb"}]),
            f"{BASE}/api/users/": FakeResponse({"results": [], "next": None}, 500),
        }
    )
    with pytest.raises(PaperlessError):
        client.fetch_desired_users("smb")


def test_group_name_is_compared_exactly():
    # the server filter ignores case, so the client must not trust it
    client, _ = make_client({f"{BASE}/api/groups/": page([{"id": 3, "name": "SMB"}])})
    with pytest.raises(PaperlessError, match="does not exist"):
        client.fetch_desired_users("smb")


def test_record_without_groups_raises_paperless_error():
    client, _ = make_client(
        {
            f"{BASE}/api/groups/": page([{"id": 3, "name": "smb"}]),
            f"{BASE}/api/users/": page(
                [{"id": 5, "username": "alice", "is_active": True}]
            ),
        }
    )
    with pytest.raises(PaperlessError, match="unexpected body"):
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
                    {
                        "id": 7,
                        "name": "[smb-sync] alice",
                        "triggers": [{"filter_path": "*/alice/*"}],
                    },
                    {"id": 8, "name": "my manual workflow"},
                ]
            )
        }
    )
    assert client.fetch_managed_workflows() == [
        ManagedWorkflow(7, "alice", "*/alice/*")
    ]


def test_workflow_without_exactly_one_trigger_reads_as_an_empty_filter():
    client, _ = make_client(
        {
            f"{BASE}/api/workflows/": page(
                [
                    {"id": 7, "name": "[smb-sync] alice", "triggers": []},
                    {
                        "id": 8,
                        "name": "[smb-sync] bob",
                        "triggers": [
                            {"filter_path": "/usr/src/paperless/consume/bob/*"},
                            {"filter_path": "*/bob/*"},
                        ],
                    },
                ]
            )
        }
    )
    assert client.fetch_managed_workflows() == [
        ManagedWorkflow(7, "alice", ""),
        ManagedWorkflow(8, "bob", ""),
    ]


def test_create_workflow_payload():
    client, session = make_client({})
    client.create_workflow(5, "alice")
    method, url, payload = session.requests[0]
    assert (method, url) == ("POST", f"{BASE}/api/workflows/")
    assert payload["name"] == "[smb-sync] alice"
    trigger = payload["triggers"][0]
    assert trigger["type"] == 1
    assert trigger["sources"] == [1]
    assert trigger["filter_path"] == "/usr/src/paperless/consume/alice/*"
    action = payload["actions"][0]
    assert action["type"] == 1
    assert action["assign_owner"] == 5
    assert action["assign_view_users"] == [5]
    assert action["assign_change_users"] == [5]


def test_delete_workflow_tolerates_404():
    session = FakeSession({}, delete_status=404)
    client = PaperlessClient(BASE, "tok", CONSUME, session=session)
    client.delete_workflow(7)
    assert session.requests == [("DELETE", f"{BASE}/api/workflows/7/", None)]


def test_update_workflow_puts_full_payload():
    client, session = make_client({})
    client.update_workflow(7, 5, "dave")
    method, url, payload = session.requests[0]
    assert (method, url) == ("PUT", f"{BASE}/api/workflows/7/")
    assert payload["name"] == "[smb-sync] dave"
    assert (
        payload["triggers"][0]["filter_path"] == "/usr/src/paperless/consume/dave/*"
    )
    assert payload["actions"][0]["assign_owner"] == 5


def test_only_the_exact_title_forms_make_a_credentials_document():
    client, _ = make_client(
        {
            f"{BASE}/api/documents/": page(
                [
                    {"id": 40, "title": "[smb-sync] credentials alice", "owner": 5},
                    {"id": 41, "title": "[SMB-SYNC] Credentials bob", "owner": None},
                    {"id": 42, "title": "[smb-sync] credentials bob (user 6)", "owner": 1},
                    {"id": 43, "title": "[smb-sync] credentials alice copy", "owner": 5},
                ]
            )
        }
    )
    assert client.fetch_credential_documents() == [
        CredentialDocument(40, "alice", 5),
        CredentialDocument(42, "bob", 1, recipient_id=6),
    ]


def test_only_the_trashed_credentials_of_that_user_are_emptied():
    client, session = make_client(
        {
            f"{BASE}/api/trash/": page(
                [
                    {"id": 40, "title": "[smb-sync] credentials alice", "owner": 5},
                    {"id": 41, "title": "[smb-sync] credentials alice (user 5)", "owner": 1},
                    {"id": 42, "title": "[smb-sync] credentials bob", "owner": 6},
                    {"id": 43, "title": "invoice", "owner": 5},
                    {"id": 44, "title": "[smb-sync] credentials dave", "owner": 5},
                ]
            )
        }
    )
    client.delete_credentials("alice", [40, 44])
    assert session.requests == [
        ("DELETE", f"{BASE}/api/documents/40/", None),
        ("DELETE", f"{BASE}/api/documents/44/", None),
        ("POST", f"{BASE}/api/trash/", {"action": "empty", "documents": [40, 41, 44]}),
    ]
    client.delete_credentials("carol", [])
    assert len(session.requests) == 3
