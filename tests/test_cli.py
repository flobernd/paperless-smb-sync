from smb_sync.cli import format_status, regen_password
from smb_sync.config import Config
from smb_sync.model import (
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


def test_regen_password_unknown_user(capsys):
    system = FakeSystem(accounts=[])
    assert regen_password(CFG, system, "alice") == 1
    assert "no managed samba account" in capsys.readouterr().err


def test_regen_password_sets_and_prints(capsys):
    system = FakeSystem(accounts=[SambaAccount("alice", 20005, True)])
    assert regen_password(CFG, system, "alice") == 0
    username, password, create = system.calls[0]
    assert (username, create) == ("alice", False)
    out = capsys.readouterr().out
    assert "SMB credentials for 'alice'" in out
    assert password in out
