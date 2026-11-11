import fcntl
from dataclasses import replace

import pytest

from smb_sync.config import Config, MailConfig
from smb_sync.model import PaperlessUser
from smb_sync.paperless import PaperlessError
from smb_sync.service import run_cycle, run_cycle_locked, run_daemon
from smb_sync.system import CommandError

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


def test_cycle_runs_while_it_holds_the_lock(tmp_path):
    lock_path = tmp_path / "lock"
    seen = []

    class ProbingApi(FakeApi):
        def fetch_desired_users(self, group_name):
            # a second open file description cannot take a held flock
            with open(lock_path, "a", encoding="utf-8") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen.append("free")
                except BlockingIOError:
                    seen.append("held")
            return []

    run_cycle_locked(CFG, ProbingApi(), FakeSystem(), lock_path=str(lock_path))
    assert seen == ["held"]


class StopLoop(Exception):
    pass


def test_daemon_survives_cycle_crash(monkeypatch, tmp_path):
    class CrashingApi:
        def fetch_desired_users(self, group_name):
            # not a PaperlessError, so it escapes run_cycle and must be
            # swallowed by the daemon loop itself
            raise RuntimeError("boom")

        def fetch_managed_workflows(self):
            return []

    def stop(_seconds):
        raise StopLoop()

    monkeypatch.setattr("smb_sync.service.time.sleep", stop)
    with pytest.raises(StopLoop):
        run_daemon(
            CFG, CrashingApi(), FakeSystem(), lock_path=str(tmp_path / "lock")
        )


MAIL_CFG = replace(
    CFG,
    email_credentials=True,
    mail=replace(NO_MAIL, host="smtp.lan", sender="scans@example.com"),
)


def test_credentials_are_emailed_when_mail_is_enabled(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(
        "smb_sync.service.send_credentials",
        lambda mail, to, username, block: sent.append((mail.host, to, username, block)),
    )
    api = FakeApi(users=[PaperlessUser(5, "alice", True, "alice@example.com")])
    assert run_cycle(MAIL_CFG, api, FakeSystem()) is True
    host, to, username, block = sent[0]
    assert (host, to, username) == ("smtp.lan", "alice@example.com", "alice")
    assert "SMB credentials for 'alice'" in block


def test_nothing_is_emailed_when_mail_is_disabled(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(
        "smb_sync.service.send_credentials", lambda *args: sent.append(args)
    )
    api = FakeApi(users=[PaperlessUser(5, "alice", True, "alice@example.com")])
    assert run_cycle(CFG, api, FakeSystem()) is True
    assert sent == []
