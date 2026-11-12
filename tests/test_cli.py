import logging
from dataclasses import replace

from smb_sync.cli import format_status, main, regen_password
from smb_sync.config import Config
from smb_sync.model import (
    ActualState,
    CredentialDocument,
    InboxFolder,
    ManagedWorkflow,
    PaperlessUser,
    SambaAccount,
    TokenUser,
)
from smb_sync.paperless import PaperlessError
from smb_sync.planner import plan_cycle

# defined locally because pytest does not import test modules as a package
CFG = Config(
    paperless_url="http://x",
    paperless_token="tok",
    sync_group="smb",
    poll_interval=30,
    uid_base=20000,
    paperless_gid=10000,
    share_name="inbox",
    smb_host="",
    consume_path="/usr/src/paperless/consume",
    credentials_document=False,
)


class FakeSystem:
    def __init__(self, accounts):
        self.accounts = accounts
        self.calls = []

    def list_samba_accounts(self):
        return self.accounts

    def set_samba_password(self, username, password, create=False):
        self.calls.append((username, password, create))


CONSUME = "/usr/src/paperless/consume"


def test_format_status_table():
    desired = [PaperlessUser(5, "alice", True), PaperlessUser(6, "bob", False)]
    actual = ActualState(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(
            ManagedWorkflow(7, "alice", "/usr/src/paperless/consume/alice/*"),
        ),
        unix_uids={},
        documents=(),
        token_user=None,
    )
    plan = plan_cycle(desired, actual, 20000, CONSUME)
    output = format_status(desired, actual, plan, 20000)
    lines = output.splitlines()
    assert "USERNAME" in lines[0]
    alice_line = next(line for line in lines if line.startswith("alice"))
    assert "enabled" in alice_line
    assert "yes" in alice_line
    bob_line = next(line for line in lines if line.startswith("bob"))
    assert "missing" in bob_line


def test_status_labels_follow_the_plan():
    desired = [
        PaperlessUser(5, "dave", True),
        PaperlessUser(6, "Bad Name", True),
        PaperlessUser(7, "carol", True),
    ]
    actual = ActualState(
        accounts=(
            SambaAccount("alice", 20005, True),
            SambaAccount("bob", 20006, True),
            SambaAccount("carol", None, True),
            SambaAccount("gone", 20009, True),
        ),
        folders=(),
        workflows=(),
        unix_uids={"alice": 20005, "bob": 20006, "gone": 20009},
        documents=(),
        token_user=None,
    )
    plan = plan_cycle(desired, actual, 20000, CONSUME)
    text = format_status(desired, actual, plan, 20000)
    rows = {line.split()[0]: line for line in text.splitlines() if line.strip()}
    assert "renaming to dave" in rows["alice"]
    assert "left untouched" in rows["bob"]
    assert "pending removal" in rows["gone"]
    assert "missing" in rows["carol"]


def test_regen_password_refuses_unmanaged_account(capsys):
    system = FakeSystem(accounts=[SambaAccount("legacy", 1000, True)])
    assert regen_password(CFG, system, "legacy", object()) == 1
    assert "no managed samba account" in capsys.readouterr().err
    assert system.calls == []


def test_regen_password_refuses_an_entry_without_stored_uid(capsys):
    system = FakeSystem(accounts=[SambaAccount("orphan", None, True)])
    assert regen_password(CFG, system, "orphan", object()) == 1
    assert "stores no uid" in capsys.readouterr().err
    assert system.calls == []


def test_regen_password_sets_and_prints(capsys):
    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    assert regen_password(CFG, system, "alice", object()) == 0
    username, password, create = system.calls[0]
    assert (username, create) == ("alice", False)
    out = capsys.readouterr().out
    assert "SMB credentials for 'alice'" in out
    assert password in out


def test_main_sync_maps_cycle_result_to_exit_code(monkeypatch):
    monkeypatch.setattr("smb_sync.cli.load_config", lambda: CFG)
    monkeypatch.setattr(
        "smb_sync.cli.PaperlessClient", lambda url, token, consume: object()
    )
    monkeypatch.setattr("smb_sync.cli.SystemAdapter", lambda root, gid: object())
    monkeypatch.setattr(
        "smb_sync.cli.run_cycle_locked", lambda cfg, api, system: True
    )
    assert main(["sync"]) == 0
    monkeypatch.setattr(
        "smb_sync.cli.run_cycle_locked", lambda cfg, api, system: False
    )
    assert main(["sync"]) == 1


def test_main_regen_unknown_account(monkeypatch, capsys):
    monkeypatch.setattr("smb_sync.cli.load_config", lambda: CFG)
    monkeypatch.setattr(
        "smb_sync.cli.PaperlessClient", lambda url, token, consume: object()
    )
    monkeypatch.setattr(
        "smb_sync.cli.SystemAdapter", lambda root, gid: FakeSystem(accounts=[])
    )
    assert main(["regen", "ghost"]) == 1
    assert "no managed samba account" in capsys.readouterr().err


def test_main_status_reports_an_api_failure_in_one_line(monkeypatch, capsys):
    class DownApi:
        def fetch_desired_users(self, group_name):
            raise PaperlessError("api down")

    monkeypatch.setattr("smb_sync.cli.load_config", lambda: CFG)
    monkeypatch.setattr(
        "smb_sync.cli.PaperlessClient", lambda url, token, consume: DownApi()
    )
    monkeypatch.setattr(
        "smb_sync.cli.SystemAdapter", lambda root, gid: FakeSystem(accounts=[])
    )
    assert main(["status"]) == 1
    err = capsys.readouterr().err
    assert "smb-sync: api down" in err.splitlines()
    assert "Traceback" not in err


class FakeDocumentApi:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []
        self.versions = []
        self.uploaded = []

    def token_user(self):
        return TokenUser(1, True)

    def fetch_credential_documents(self):
        return self.documents

    def hand_over_document(self, document_id, user_id, username):
        self.calls.append(("hand_over_document", document_id, user_id, username))

    def add_credentials_version(self, document_id, block):
        self.calls.append(("add_credentials_version", document_id))
        self.versions.append((document_id, block))

    def upload_credentials(self, user_id, username, block):
        self.uploaded.append((user_id, username))


DOCUMENT_CFG = replace(CFG, credentials_document=True)


def test_regen_adds_a_version_to_the_document_of_the_user(capsys):
    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    api = FakeDocumentApi(
        [
            CredentialDocument(41, "alice", 9, recipient_id=None),
            CredentialDocument(40, "alice", 5, recipient_id=None),
        ]
    )
    assert regen_password(DOCUMENT_CFG, system, "alice", api) == 0
    (document_id, block), = api.versions
    assert document_id == 40
    assert system.calls[0][1] in block
    assert "smb-sync regen" not in block
    assert api.uploaded == []


def test_regen_uploads_a_document_when_none_is_left(capsys):
    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    api = FakeDocumentApi([])
    assert regen_password(DOCUMENT_CFG, system, "alice", api) == 0
    assert api.uploaded == [(5, "alice")]


def test_regen_of_the_token_user_leaves_the_uploads_for_other_users(capsys):
    system = FakeSystem(accounts=[SambaAccount("admin", 20001, True)])
    api = FakeDocumentApi(
        [
            CredentialDocument(41, "bob", 1, recipient_id=6),
            CredentialDocument(40, "admin", 1, recipient_id=None),
        ]
    )
    assert regen_password(DOCUMENT_CFG, system, "admin", api) == 0
    (document_id, _), = api.versions
    assert document_id == 40


def test_regen_resets_the_rights_of_the_document_before_it_writes(capsys):
    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    api = FakeDocumentApi([CredentialDocument(40, "alice", 5, recipient_id=None)])
    assert regen_password(DOCUMENT_CFG, system, "alice", api) == 0
    assert [call[:2] for call in api.calls] == [
        ("hand_over_document", 40),
        ("add_credentials_version", 40),
    ]


def test_regen_survives_an_api_failure_at_the_document_update(caplog):
    class DownApi:
        def token_user(self):
            raise PaperlessError("api down")

    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    with caplog.at_level(logging.ERROR):
        assert regen_password(DOCUMENT_CFG, system, "alice", DownApi()) == 0
    assert "api down" in caplog.text
