import fcntl
import logging
import re
from dataclasses import replace

import pytest

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
from smb_sync.system import CommandError

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


JOINED = 1731405600123
ALICE = ("!u20005 = alice", "!nobody = *")


def member(user_id, username, active=True):
    return PaperlessUser(user_id, username, active, JOINED, True)


def entry(user_id, enabled=True):
    uid = 20000 + user_id
    return SambaAccount(f"u{uid}", f"smb-sync uid={uid} joined={JOINED}", enabled)


class FakeSystem:
    def __init__(self, accounts, lines=None):
        self.accounts = accounts
        self.lines = lines
        self.calls = []

    def list_samba_accounts(self):
        return self.accounts

    def read_usermap(self):
        return self.lines

    def set_samba_password(self, account, password, create=False):
        self.calls.append((account, password, create))

    def disable_samba_account(self, account):
        self.calls.append(("disable_samba_account", account))


def regen(system, login_name, api, tmp_path, cfg=CFG):
    return regen_password(cfg, system, login_name, api, str(tmp_path / "lock"))


def test_status_rows_follow_the_plan():
    users = [
        # a paperless user outside the sync group, who has no row
        PaperlessUser(4, "zed", True, JOINED, False),
        member(5, "alice"),
        member(6, "a&b"),
        member(7, "carol"),
        member(8, "dave", active=False),
    ]
    actual = ActualState(
        accounts=(entry(5), entry(6), entry(8, enabled=False), entry(9)),
        folders=(InboxFolder("u20005", 20005, 10000),),
        workflows=(ManagedWorkflow(105, "u20005", 5),),
        unix_names=frozenset(),
        session_accounts=frozenset(),
        map_lines=("!u20005 = alice", "!u20009 = erin", "!nobody = *"),
        documents=(),
        token_user=None,
    )
    plan = plan_cycle(users, actual, 20000, 10000)
    table = format_status(users, actual, plan, 20000).splitlines()[1:6]
    assert [re.split(r" {2,}", row) for row in table] == [
        ["alice", "u20005", "20005", "open", "yes", "yes"],
        ["a&b", "u20006", "20006", "no login name", "no", "no"],
        ["carol", "u20007", "20007", "missing", "no", "no"],
        ["dave", "u20008", "20008", "closed", "no", "no"],
        ["erin", "u20009", "20009", "pending removal", "no", "no"],
    ]


def test_status_of_a_hold_shows_the_login_of_an_entry_without_a_member():
    # paperless knows none of the entries, so the removal of u20005 waits
    users = [member(1, "anna")]
    actual = ActualState(
        accounts=(entry(5),),
        folders=(),
        workflows=(),
        unix_names=frozenset(),
        session_accounts=frozenset(),
        map_lines=("!u20005 = alice", "!nobody = *"),
        documents=(),
        token_user=None,
    )
    plan = plan_cycle(users, actual, 20000, 10000)
    table = format_status(users, actual, plan, 20000).splitlines()[1:3]
    assert [re.split(r" {2,}", row) for row in table] == [
        ["anna", "u20001", "20001", "missing", "no", "no"],
        ["alice", "u20005", "20005", "open", "no", "no"],
    ]


def test_regen_finds_the_account_through_the_map_without_case(tmp_path, capsys):
    system = FakeSystem(
        [entry(5), entry(9)], ("!u20005 = John.Doe", "!u20009 = anna", "!nobody = *")
    )
    assert regen(system, "john.doe", object(), tmp_path) == 0
    ((account, password, create),) = system.calls
    assert (account, create) == ("u20005", False)
    out = capsys.readouterr().out
    assert "SMB credentials for 'John.Doe'" in out
    assert password in out


@pytest.mark.parametrize(
    ("typed", "accounts"),
    [
        # the account name is no login name
        ("u20005", [entry(5)]),
        # the line of the map names an entry that the daemon does not manage
        ("ghost", [entry(5), SambaAccount("u20007", "", True)]),
    ],
)
def test_regen_refuses_a_name_without_a_managed_account(typed, accounts, tmp_path, capsys):
    system = FakeSystem(accounts, ("!u20005 = alice", "!u20007 = ghost", "!nobody = *"))
    assert regen(system, typed, object(), tmp_path) == 1
    assert typed in capsys.readouterr().err
    assert system.calls == []


def test_regen_holds_the_cycle_lock_from_the_map_read_to_the_document(tmp_path):
    seen = []

    def probe():
        # a second open file description cannot take a held flock
        with open(tmp_path / "lock", "a", encoding="utf-8") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except BlockingIOError:
                seen.append("held")

    class ProbingSystem(FakeSystem):
        def read_usermap(self):
            probe()
            return self.lines

    class ProbingApi(FakeDocumentApi):
        def upload_credentials(self, user_id, username, block):
            probe()

    system = ProbingSystem([entry(5)], ALICE)
    assert regen(system, "alice", ProbingApi([]), tmp_path, cfg=DOCUMENT_CFG) == 0
    assert seen == ["held", "held"]


def test_regen_keeps_a_disabled_login_closed(tmp_path):
    system = FakeSystem([entry(5, enabled=False)], ALICE)
    assert regen(system, "alice", object(), tmp_path) == 0
    (username, _, create), disable = system.calls
    assert (username, create) == ("u20005", False)
    assert disable == ("disable_samba_account", "u20005")
    # an open login stays open
    system = FakeSystem([entry(5)], ALICE)
    assert regen(system, "alice", object(), tmp_path) == 0
    assert len(system.calls) == 1


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


def test_main_regen_unknown_account(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("smb_sync.cli.load_config", lambda: CFG)
    monkeypatch.setattr(
        "smb_sync.cli.PaperlessClient", lambda url, token, consume: object()
    )
    monkeypatch.setattr(
        "smb_sync.cli.SystemAdapter", lambda root, gid: FakeSystem(accounts=[])
    )
    monkeypatch.setattr("smb_sync.cli.DEFAULT_LOCK_PATH", str(tmp_path / "lock"))
    assert main(["regen", "ghost"]) == 1
    assert "ghost" in capsys.readouterr().err


def test_main_regen_reports_a_failed_command_in_one_line(monkeypatch, tmp_path, capsys):
    class BrokenSystem(FakeSystem):
        def read_usermap(self):
            raise CommandError("cannot read the username map")

    monkeypatch.setattr("smb_sync.cli.load_config", lambda: CFG)
    monkeypatch.setattr(
        "smb_sync.cli.PaperlessClient", lambda url, token, consume: object()
    )
    monkeypatch.setattr(
        "smb_sync.cli.SystemAdapter", lambda root, gid: BrokenSystem(accounts=[])
    )
    monkeypatch.setattr("smb_sync.cli.DEFAULT_LOCK_PATH", str(tmp_path / "lock"))
    assert main(["regen", "alice"]) == 1
    err = capsys.readouterr().err
    assert "smb-sync: cannot read the username map" in err.splitlines()


def test_main_status_reports_an_api_failure_in_one_line(monkeypatch, capsys):
    class DownApi:
        def fetch_users(self, group_name):
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


def test_regen_adds_a_version_to_the_document_of_the_user(tmp_path, capsys):
    system = FakeSystem([entry(5)], ALICE)
    api = FakeDocumentApi(
        [
            CredentialDocument(41, "alice", 9, recipient_id=None),
            CredentialDocument(40, "alice", 5, recipient_id=None),
        ]
    )
    assert regen(system, "alice", api, tmp_path, cfg=DOCUMENT_CFG) == 0
    (document_id, block), = api.versions
    assert document_id == 40
    assert system.calls[0][1] in block
    assert "smb-sync regen" not in block
    assert api.uploaded == []


def test_regen_uploads_a_document_when_none_is_left(tmp_path, capsys):
    system = FakeSystem([entry(5)], ALICE)
    api = FakeDocumentApi([])
    assert regen(system, "alice", api, tmp_path, cfg=DOCUMENT_CFG) == 0
    assert api.uploaded == [(5, "alice")]


def test_regen_of_the_token_user_leaves_the_uploads_for_other_users(tmp_path, capsys):
    system = FakeSystem([entry(1)], ("!u20001 = admin", "!nobody = *"))
    api = FakeDocumentApi(
        [
            CredentialDocument(41, "bob", 1, recipient_id=6),
            CredentialDocument(40, "admin", 1, recipient_id=None),
        ]
    )
    assert regen(system, "admin", api, tmp_path, cfg=DOCUMENT_CFG) == 0
    (document_id, _), = api.versions
    assert document_id == 40


def test_regen_resets_the_rights_of_the_document_before_it_writes(tmp_path, capsys):
    system = FakeSystem([entry(5)], ALICE)
    api = FakeDocumentApi([CredentialDocument(40, "alice", 5, recipient_id=None)])
    assert regen(system, "alice", api, tmp_path, cfg=DOCUMENT_CFG) == 0
    assert [call[:2] for call in api.calls] == [
        ("hand_over_document", 40),
        ("add_credentials_version", 40),
    ]


def test_regen_survives_an_api_failure_at_the_document_update(tmp_path, caplog):
    class DownApi:
        def token_user(self):
            raise PaperlessError("api down")

    system = FakeSystem([entry(5)], ALICE)
    with caplog.at_level(logging.ERROR):
        assert regen(system, "alice", DownApi(), tmp_path, cfg=DOCUMENT_CFG) == 0
    assert "api down" in caplog.text
