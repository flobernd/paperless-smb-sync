from smb_sync.cli import format_status, main, regen_password
from smb_sync.config import Config
from smb_sync.model import (
    UNMAPPED_UID,
    ActualState,
    InboxFolder,
    ManagedWorkflow,
    PaperlessUser,
    SambaAccount,
)

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
    inbox_path="/shares/inbox",
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
