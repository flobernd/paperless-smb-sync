import logging
from dataclasses import replace

import pytest

from smb_sync.cli import format_status, main, regen_password
from smb_sync.config import Config, MailConfig
from smb_sync.model import (
    ActualState,
    InboxFolder,
    ManagedWorkflow,
    PaperlessUser,
    SambaAccount,
)
from smb_sync.paperless import PaperlessError
from smb_sync.planner import plan_cycle

# defined locally because pytest does not import test modules as a package
NO_MAIL = MailConfig(
    host="",
    port=25,
    username="",
    password="",
    use_tls=False,
    use_ssl=False,
    sender="smb-sync@localhost",
)

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
    email_credentials=False,
    mail=NO_MAIL,
)


class FakeSystem:
    def __init__(self, accounts):
        self.accounts = accounts
        self.calls = []

    def list_samba_accounts(self):
        return self.accounts

    def set_samba_password(self, username, password, create=False):
        self.calls.append((username, password, create))


class FakeUserApi:
    def __init__(self, users):
        self.users = users

    def fetch_desired_users(self, group_name):
        return self.users


CONSUME = "/usr/src/paperless/consume"


def test_format_status_table():
    desired = [PaperlessUser(5, "alice", True), PaperlessUser(6, "bob", False)]
    actual = ActualState(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(
            ManagedWorkflow(7, "alice", "/usr/src/paperless/consume/alice/*"),
        ),
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
    assert regen_password(CFG, system, "legacy", FakeUserApi([])) == 1
    assert "no managed samba account" in capsys.readouterr().err
    assert system.calls == []


def test_regen_password_refuses_an_entry_without_stored_uid(capsys):
    system = FakeSystem(accounts=[SambaAccount("orphan", None, True)])
    assert regen_password(CFG, system, "orphan", FakeUserApi([])) == 1
    assert "stores no uid" in capsys.readouterr().err
    assert system.calls == []


def test_regen_password_sets_and_prints(capsys):
    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    assert regen_password(CFG, system, "alice", FakeUserApi([])) == 0
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


MAIL_CFG = replace(
    CFG,
    email_credentials=True,
    mail=replace(NO_MAIL, host="smtp.lan", sender="scans@example.com"),
)


def test_regen_emails_the_new_password(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(
        "smb_sync.cli.send_credentials",
        lambda mail, to, username, block: sent.append((mail.host, to, username, block)),
    )
    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    api = FakeUserApi([PaperlessUser(5, "alice", True, "alice@example.com")])
    assert regen_password(MAIL_CFG, system, "alice", api) == 0
    host, to, username, block = sent[0]
    assert (host, to, username) == ("smtp.lan", "alice@example.com", "alice")
    assert system.calls[0][1] in block


def test_regen_does_not_email_when_mail_is_disabled(monkeypatch):
    monkeypatch.setattr(
        "smb_sync.cli.send_credentials",
        lambda *args: pytest.fail("mail is disabled"),
    )
    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    api = FakeUserApi([PaperlessUser(5, "alice", True, "alice@example.com")])
    assert regen_password(CFG, system, "alice", api) == 0


def test_main_logs_config_warnings(monkeypatch, caplog):
    monkeypatch.setattr(
        "smb_sync.cli.load_config",
        lambda: replace(CFG, email_credentials=True),
    )
    monkeypatch.setattr(
        "smb_sync.cli.PaperlessClient", lambda url, token, consume: None
    )
    monkeypatch.setattr("smb_sync.cli.SystemAdapter", lambda path, gid: None)
    monkeypatch.setattr("smb_sync.cli.run_daemon", lambda cfg, api, system: None)
    with caplog.at_level(logging.WARNING):
        assert main(["run"]) == 0
    assert "PAPERLESS_EMAIL_HOST" in caplog.text


def test_regen_survives_an_api_failure_during_the_email_lookup(monkeypatch, caplog):
    class BrokenApi:
        def fetch_desired_users(self, group_name):
            raise PaperlessError("api down")

    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    with caplog.at_level(logging.ERROR):
        # the password is already changed by this point, so a lookup failure
        # must not surface as a crash
        assert regen_password(MAIL_CFG, system, "alice", BrokenApi()) == 0
    assert "api down" in caplog.text
