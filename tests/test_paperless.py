import http.server
import threading

import pytest
import requests

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
    """Routes by exact URL and records every write request."""

    def __init__(self, routes, delete_status=204, writes=None):
        self.routes = routes
        self.delete_status = delete_status
        self.writes = writes or {}
        self.headers = {}
        self.requests = []

    def request(self, method, url, **kwargs):
        if method == "GET":
            return self.routes[url]
        self.requests.append((method, url, kwargs.get("json")))
        if (method, url) in self.writes:
            return self.writes[(method, url)]
        status = {"DELETE": self.delete_status, "POST": 201, "PUT": 200}[method]
        return FakeResponse({}, status)


BASE = "http://paperless:8000"
CONSUME = "/data/consume"


def page(results, next_url=None):
    return FakeResponse({"results": results, "next": next_url})


def make_client(routes, writes=None):
    session = FakeSession(routes, writes=writes)
    return PaperlessClient(BASE, "tok", CONSUME, session=session), session


def test_each_user_carries_the_member_flag_and_the_join_date():
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
                        "date_joined": "2024-11-12T10:00:00.123999Z",
                    },
                    # paperless prints the offset of its time zone setting
                    {
                        "id": 6,
                        "username": "bob",
                        "is_active": False,
                        "groups": [3, 4],
                        "date_joined": "2024-11-12T11:00:00.123999+01:00",
                    },
                    {
                        "id": 7,
                        "username": "carol",
                        "is_active": True,
                        "groups": [],
                        "date_joined": "2024-11-12T10:00:00Z",
                    },
                ]
            ),
        }
    )
    assert client.fetch_users("smb") == [
        PaperlessUser(5, "alice", True, 1731405600123, True),
        PaperlessUser(6, "bob", False, 1731405600123, True),
        PaperlessUser(7, "carol", True, 1731405600000, False),
    ]


def test_pagination_followed():
    client, _ = make_client(
        {
            f"{BASE}/api/groups/": page(
                [{"id": 1, "name": "other"}], next_url=f"{BASE}/api/groups/?page=2"
            ),
            f"{BASE}/api/groups/?page=2": page([{"id": 3, "name": "smb"}]),
            f"{BASE}/api/users/": page(
                [
                    {
                        "id": 5,
                        "username": "alice",
                        "is_active": True,
                        "groups": [3],
                        "date_joined": "2024-11-12T10:00:00Z",
                    }
                ]
            ),
        }
    )
    users = client.fetch_users("smb")
    assert len(users) == 1


def test_missing_group_raises():
    client, _ = make_client({f"{BASE}/api/groups/": page([])})
    with pytest.raises(PaperlessError, match="does not exist"):
        client.fetch_users("smb")


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
        client.fetch_users("smb")


def test_group_name_is_compared_exactly():
    # the server filter ignores case, so the client must not trust it
    client, _ = make_client({f"{BASE}/api/groups/": page([{"id": 3, "name": "SMB"}])})
    with pytest.raises(PaperlessError, match="does not exist"):
        client.fetch_users("smb")


def test_record_without_groups_raises_paperless_error():
    client, _ = make_client(
        {
            f"{BASE}/api/groups/": page([{"id": 3, "name": "smb"}]),
            f"{BASE}/api/users/": page(
                [
                    {
                        "id": 5,
                        "username": "alice",
                        "is_active": True,
                        "date_joined": "2024-11-12T10:00:00Z",
                    }
                ]
            ),
        }
    )
    with pytest.raises(PaperlessError, match="unexpected body"):
        client.fetch_users("smb")


def test_non_json_body_raises_paperless_error():
    client, _ = make_client({f"{BASE}/api/groups/": BrokenJsonResponse()})
    with pytest.raises(PaperlessError, match="unexpected body"):
        client.fetch_users("smb")


def test_missing_results_key_raises_paperless_error():
    client, _ = make_client({f"{BASE}/api/groups/": FakeResponse({"next": None})})
    with pytest.raises(PaperlessError, match="unexpected body"):
        client.fetch_users("smb")


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
                            {"filter_path": "/data/consume/bob/*"},
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
    assert trigger["filter_path"] == "/data/consume/alice/*"
    action = payload["actions"][0]
    assert action["type"] == 1
    assert action["assign_owner"] == 5
    assert action["assign_view_users"] == [5]
    assert action["assign_change_users"] == [5]


def test_a_redirected_write_fails():
    # A real session, since the session fake cannot follow a redirect. It
    # ignores the proxy variables of the environment, which would send the
    # request to a proxy in place of the local server.
    session = requests.Session()
    session.trust_env = False
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(("GET", self.path))
            body = b'{"count": 0, "next": null, "previous": null, "results": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            seen.append(("POST", self.path))
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(301)
            self.send_header("Location", f"{url}{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    client = PaperlessClient(url, "token", CONSUME, session=session)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with pytest.raises(PaperlessError):
            client.create_workflow(5, "alice")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert seen == [("POST", "/api/workflows/")]


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
    assert payload["triggers"][0]["filter_path"] == "/data/consume/dave/*"
    assert payload["actions"][0]["assign_owner"] == 5


UPLOAD = "smb-credentials.txt"


def test_only_the_exact_title_forms_make_a_credentials_document():
    client, _ = make_client(
        {
            f"{BASE}/api/documents/": page(
                [
                    {
                        "id": 40,
                        "title": "[smb-sync] credentials alice",
                        "owner": 5,
                        "original_file_name": UPLOAD,
                    },
                    {
                        "id": 41,
                        "title": "[SMB-SYNC] Credentials bob",
                        "owner": None,
                        "original_file_name": UPLOAD,
                    },
                    {
                        "id": 42,
                        "title": "[smb-sync] credentials bob (user 6)",
                        "owner": 1,
                        "original_file_name": UPLOAD,
                    },
                    {
                        "id": 43,
                        "title": "[smb-sync] credentials alice copy",
                        "owner": 5,
                        "original_file_name": UPLOAD,
                    },
                    # a title that a user with the change right gave a scan
                    {
                        "id": 44,
                        "title": "[smb-sync] credentials carol",
                        "owner": None,
                        "original_file_name": "scan.pdf",
                    },
                ]
            )
        }
    )
    assert client.fetch_credential_documents() == [
        CredentialDocument(40, "alice", 5, recipient_id=None),
        CredentialDocument(42, "bob", 1, recipient_id=6),
    ]


def test_only_the_trashed_credentials_of_that_user_are_emptied():
    client, session = make_client(
        {
            f"{BASE}/api/trash/": page(
                [
                    {
                        "id": 40,
                        "title": "[smb-sync] credentials alice",
                        "owner": 5,
                        "original_file_name": UPLOAD,
                    },
                    # an upload before its hand-over to user 5
                    {
                        "id": 41,
                        "title": "[smb-sync] credentials alice (user 5)",
                        "owner": 1,
                        "original_file_name": UPLOAD,
                    },
                    # user 9 holds the name now
                    {
                        "id": 42,
                        "title": "[smb-sync] credentials alice",
                        "owner": 9,
                        "original_file_name": UPLOAD,
                    },
                    {
                        "id": 43,
                        "title": "invoice",
                        "owner": 5,
                        "original_file_name": UPLOAD,
                    },
                    # user 5 had the name dave before
                    {
                        "id": 44,
                        "title": "[smb-sync] credentials dave",
                        "owner": 5,
                        "original_file_name": UPLOAD,
                    },
                    {
                        "id": 45,
                        "title": "[smb-sync] credentials alice",
                        "owner": None,
                        "original_file_name": UPLOAD,
                    },
                ]
            )
        }
    )
    client.delete_credentials("alice", 5, [40])
    assert session.requests == [
        ("DELETE", f"{BASE}/api/documents/40/", None),
        (
            "POST",
            f"{BASE}/api/trash/",
            {"action": "empty", "documents": [40, 41, 44, 45]},
        ),
    ]
    client.delete_credentials("carol", 7, [])
    assert len(session.requests) == 2


def test_a_note_goes_only_to_a_document_without_it():
    note = "The username changed from 'dave' to 'erin'. The password is the same."
    earlier = "The username changed from 'carol' to 'dave'. The password is the same."
    client, session = make_client(
        {
            f"{BASE}/api/documents/40/notes/": FakeResponse(
                [{"id": 1, "note": earlier}, {"id": 2, "note": note}]
            ),
            f"{BASE}/api/documents/41/notes/": FakeResponse([{"id": 3, "note": earlier}]),
        },
        writes={
            ("POST", f"{BASE}/api/documents/41/notes/"): FakeResponse(
                [{"id": 3, "note": earlier}, {"id": 4, "note": note}]
            )
        },
    )
    client.ensure_document_note(40, note)
    client.ensure_document_note(41, note)
    assert session.requests == [
        ("POST", f"{BASE}/api/documents/41/notes/", {"note": note})
    ]


def test_a_note_that_paperless_did_not_save_fails():
    notes = f"{BASE}/api/documents/41/notes/"
    client, _ = make_client(
        {notes: FakeResponse([])},
        writes={
            ("POST", notes): FakeResponse(
                {"error": "Error saving note, check logs for more detail."}
            )
        },
    )
    with pytest.raises(PaperlessError, match="41"):
        client.ensure_document_note(
            41,
            "The username changed from 'dave' to 'erin'. The password is the same.",
        )


def test_a_notes_read_that_answers_an_error_object_fails_and_posts_nothing():
    notes = f"{BASE}/api/documents/41/notes/"
    client, session = make_client(
        {notes: FakeResponse({"error": "Error retrieving notes, check logs for more detail."})}
    )
    with pytest.raises(PaperlessError, match="unexpected body"):
        client.ensure_document_note(
            41, "The username changed from 'dave' to 'erin'. The password is the same."
        )
    assert session.requests == []


def test_delete_document_also_empties_it_from_the_trash():
    # a document that is already in the trash answers the DELETE with 404
    session = FakeSession({}, delete_status=404)
    client = PaperlessClient(BASE, "tok", CONSUME, session=session)
    client.delete_document(40)
    assert session.requests == [
        ("DELETE", f"{BASE}/api/documents/40/", None),
        ("POST", f"{BASE}/api/trash/", {"action": "empty", "documents": [40]}),
    ]
