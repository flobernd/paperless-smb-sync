import logging
from dataclasses import replace

import pytest

from smb_sync.cli import format_status, main, regen_password
from smb_sync.config import Config, MailConfig
from smb_sync.model import (
    UNMAPPED_UID,
    ActualState,
    InboxFolder,
    ManagedWorkflow,
    PaperlessUser,
    SambaAccount,
)
from smb_sync.paperless import PaperlessError

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
    inbox_path="/shares/inbox",
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


def test_format_status_table():
    desired = [PaperlessUser(5, "alice", True), PaperlessUser(6, "bob", False)]
    actual = ActualState(
        accounts=(
            SambaAccount("alice", 20005, True),
            SambaAccount("gone", 20009, True),
        ),
        folders=(InboxFolder("alice", 20005),),
        workflows=(ManagedWorkflow(7, "alice"),),
    )
    output = format_status(desired, actual, 20000)
    lines = output.splitlines()
    assert "USERNAME" in lines[0]
    alice_line = next(line for line in lines if line.startswith("alice"))
    assert "enabled" in alice_line
    assert "yes" in alice_line
    bob_line = next(line for line in lines if line.startswith("bob"))
    assert "missing" in bob_line
    gone_line = next(line for line in lines if line.startswith("gone"))
    assert "pending removal" in gone_line


def test_format_status_labels_stranded_account():
    # a sentinel-uid account that matches no desired user is never removed by
    # the planner, so it must not read as "pending removal"
    desired = [PaperlessUser(5, "alice", True)]
    actual = ActualState(
        accounts=(
            SambaAccount("alice", 20005, True),
            SambaAccount("orphan", UNMAPPED_UID, True),
        ),
        folders=(),
        workflows=(),
    )
    output = format_status(desired, actual, 20000)
    orphan_line = next(
        line for line in output.splitlines() if line.startswith("orphan")
    )
    assert "stranded" in orphan_line
    assert "pending removal" not in orphan_line


def test_regen_password_unknown_user(capsys):
    system = FakeSystem(accounts=[])
    assert regen_password(CFG, system, "alice") == 1
    assert "no managed samba account" in capsys.readouterr().err


def test_regen_password_refuses_unmanaged_account(capsys):
    system = FakeSystem(accounts=[SambaAccount("legacy", 1000, True)])
    assert regen_password(CFG, system, "legacy") == 1
    assert "no managed samba account" in capsys.readouterr().err
    assert system.calls == []


def test_regen_password_refuses_stranded_account(capsys):
    system = FakeSystem(accounts=[SambaAccount("orphan", UNMAPPED_UID, True)])
    assert regen_password(CFG, system, "orphan") == 1
    assert "stranded" in capsys.readouterr().err
    assert system.calls == []


def test_regen_password_sets_and_prints(capsys):
    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    assert regen_password(CFG, system, "alice") == 0
    username, password, create = system.calls[0]
    assert (username, create) == ("alice", False)
    out = capsys.readouterr().out
    assert "SMB credentials for 'alice'" in out
    assert password in out


def test_main_sync_maps_cycle_result_to_exit_code(monkeypatch):
    monkeypatch.setattr("smb_sync.cli.load_config", lambda: CFG)
    monkeypatch.setattr("smb_sync.cli.PaperlessClient", lambda url, token: object())
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
    monkeypatch.setattr("smb_sync.cli.PaperlessClient", lambda url, token: object())
    monkeypatch.setattr(
        "smb_sync.cli.SystemAdapter", lambda root, gid: FakeSystem(accounts=[])
    )
    assert main(["regen", "ghost"]) == 1
    assert "no managed samba account" in capsys.readouterr().err


MAIL_CFG = replace(
    CFG,
    email_credentials=True,
    mail=replace(NO_MAIL, host="smtp.lan", sender="scans@example.com"),
)


class FakeUserApi:
    def __init__(self, users):
        self.users = users

    def fetch_desired_users(self, group_name):
        return self.users


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


def test_regen_warns_when_the_user_has_no_email(monkeypatch, caplog):
    monkeypatch.setattr(
        "smb_sync.cli.send_credentials",
        lambda *args: pytest.fail("must not send without an address"),
    )
    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    api = FakeUserApi([PaperlessUser(5, "alice", True, "")])
    with caplog.at_level(logging.WARNING):
        assert regen_password(MAIL_CFG, system, "alice", api) == 0
    assert "no email address" in caplog.text


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
    monkeypatch.setattr("smb_sync.cli.PaperlessClient", lambda url, token: None)
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


def test_regen_emails_a_block_without_the_admin_only_hint(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(
        "smb_sync.cli.send_credentials",
        lambda mail, to, username, block: sent.append(block),
    )
    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    api = FakeUserApi([PaperlessUser(5, "alice", True, "alice@example.com")])
    assert regen_password(MAIL_CFG, system, "alice", api) == 0
    assert "smb-sync regen" not in sent[0]
    assert "smb-sync regen alice" in capsys.readouterr().out
