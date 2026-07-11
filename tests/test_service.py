from smb_sync.config import Config
from smb_sync.model import PaperlessUser
from smb_sync.paperless import PaperlessError
from smb_sync.service import run_cycle, run_cycle_locked
from smb_sync.system import CommandError

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


class FakeApi:
    def __init__(self, users=None, fail=False):
        self.users = users or []
        self.fail = fail
        self.created = []

    def fetch_desired_users(self, group_name):
        if self.fail:
            raise PaperlessError("api down")
        return self.users

    def fetch_managed_workflows(self):
        return []

    def create_workflow(self, user_id, username):
        self.created.append(username)


class FakeSystem:
    def __init__(self, fail_listing=False):
        self.fail_listing = fail_listing
        self.calls = []

    def list_samba_accounts(self):
        if self.fail_listing:
            raise CommandError("pdbedit broken")
        return []

    def list_inbox_folders(self):
        return []

    def create_unix_user(self, username, uid):
        self.calls.append(("create_unix_user", username, uid))

    def set_samba_password(self, username, password, create=False):
        self.calls.append(("set_samba_password", username))

    def create_inbox_folder(self, username, uid):
        self.calls.append(("create_inbox_folder", username))


def test_api_failure_skips_cycle_without_touching_system():
    api = FakeApi(fail=True)
    system = FakeSystem()
    assert run_cycle(CFG, api, system) is False
    assert system.calls == []


def test_local_state_failure_skips_cycle():
    api = FakeApi(users=[PaperlessUser(5, "alice", True)])
    system = FakeSystem(fail_listing=True)
    assert run_cycle(CFG, api, system) is False
    assert system.calls == []


def test_happy_path_provisions(capsys):
    api = FakeApi(users=[PaperlessUser(5, "alice", True)])
    system = FakeSystem()
    assert run_cycle(CFG, api, system) is True
    assert ("create_unix_user", "alice", 20005) in system.calls
    assert ("create_inbox_folder", "alice") in system.calls
    assert api.created == ["alice"]


def test_run_cycle_locked_uses_lock_file(tmp_path, capsys):
    lock_path = tmp_path / "lock"
    api = FakeApi(users=[])
    system = FakeSystem()
    assert run_cycle_locked(CFG, api, system, lock_path=str(lock_path)) is True
    assert lock_path.exists()
