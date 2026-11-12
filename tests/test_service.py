import fcntl
import logging
import os
from dataclasses import replace

import pytest

from smb_sync.config import Config
from smb_sync.model import PaperlessUser, SambaAccount, TokenUser
from smb_sync.paperless import PaperlessError
from smb_sync.service import (
    WarningRepeat,
    heartbeat_is_fresh,
    restore_unix_accounts,
    run_cycle,
    run_cycle_locked,
    run_daemon,
)
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
    consume_path="/usr/src/paperless/consume",
    credentials_document=False,
)


class FakeApi:
    def __init__(self, users=None, fail=False, token_user=None):
        self.users = users or []
        self.fail = fail
        self.created = []
        self._token_user = token_user
        self.uploaded = []

    def fetch_desired_users(self, group_name):
        if self.fail:
            raise PaperlessError("api down")
        return self.users

    def fetch_managed_workflows(self):
        return []

    def token_user(self):
        return self._token_user

    def fetch_credential_documents(self):
        return []

    def create_workflow(self, user_id, username):
        self.created.append(username)

    def upload_credentials(self, user_id, username, block):
        self.uploaded.append((user_id, username))


class FakeSystem:
    def __init__(self, fail_listing=False, accounts=(), fail_user=None):
        self.fail_listing = fail_listing
        self.accounts = accounts
        self.fail_user = fail_user
        self.calls = []

    def list_samba_accounts(self):
        if self.fail_listing:
            raise CommandError("pdbedit broken")
        return list(self.accounts)

    def list_inbox_folders(self):
        return []

    def list_unix_uids(self):
        return {}

    def create_unix_user(self, username, uid):
        if username == self.fail_user:
            raise CommandError(f"uid {uid} is taken")
        self.calls.append(("create_unix_user", username, uid))

    def set_samba_password(self, username, password, create=False):
        self.calls.append(("set_samba_password", username))

    def store_uid(self, username, uid):
        self.calls.append(("store_uid", username, uid))

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
            CFG,
            CrashingApi(),
            FakeSystem(),
            lock_path=str(tmp_path / "lock"),
            heartbeat_path=str(tmp_path / "heartbeat"),
        )
    assert (tmp_path / "heartbeat").exists()


def test_standing_warning_is_due_again_after_the_interval():
    now = [0.0]
    repeat = WarningRepeat(clock=lambda: now[0])
    assert repeat.due(["w"]) == ["w"]
    now[0] = 3599.0
    assert repeat.due(["w"]) == []
    now[0] = 3600.0
    assert repeat.due(["w"]) == ["w"]


def test_daemon_logs_a_standing_warning_once(monkeypatch, tmp_path, caplog):
    cycles = [
        [PaperlessUser(5, "Bad Name", True)],
        [PaperlessUser(5, "Bad Name", True), PaperlessUser(6, "Other Bad", True)],
    ]

    class ChangingApi(FakeApi):
        def fetch_desired_users(self, group_name):
            return cycles.pop(0)

    def sleep(_seconds):
        if not cycles:
            raise StopLoop()

    monkeypatch.setattr("smb_sync.service.time.sleep", sleep)
    with caplog.at_level(logging.WARNING), pytest.raises(StopLoop):
        run_daemon(
            CFG,
            ChangingApi(),
            FakeSystem(),
            lock_path=str(tmp_path / "lock"),
            heartbeat_path=str(tmp_path / "heartbeat"),
        )
    assert caplog.text.count("'Bad Name'") == 1
    assert caplog.text.count("'Other Bad'") == 1


def test_cycle_without_repeat_logs_a_standing_warning(caplog):
    api = FakeApi(users=[PaperlessUser(5, "Bad Name", True)])
    with caplog.at_level(logging.WARNING):
        run_cycle(CFG, api, FakeSystem())
    assert "'Bad Name'" in caplog.text


DOCUMENT_CFG = replace(CFG, credentials_document=True)


def test_only_a_superuser_token_uploads_a_document(caplog):
    users = [PaperlessUser(5, "alice", True)]
    admin = FakeApi(users=users, token_user=TokenUser(1, True))
    assert run_cycle(DOCUMENT_CFG, admin, FakeSystem()) is True
    assert admin.uploaded == [(5, "alice")]

    plain = FakeApi(users=users, token_user=TokenUser(1, False))
    with caplog.at_level(logging.WARNING):
        assert run_cycle(DOCUMENT_CFG, plain, FakeSystem()) is True
    assert plain.uploaded == []
    assert "superuser" in caplog.text


class NoTokenUser(FakeApi):
    def token_user(self):
        raise PaperlessError("documents down")


class NoDocumentList(FakeApi):
    def fetch_credential_documents(self):
        raise PaperlessError("documents down")


@pytest.mark.parametrize("api_class", [NoTokenUser, NoDocumentList])
def test_a_failed_document_read_does_not_stop_the_cycle(api_class, caplog):
    api = api_class(
        users=[PaperlessUser(5, "alice", True)], token_user=TokenUser(1, True)
    )
    with caplog.at_level(logging.ERROR):
        assert run_cycle(DOCUMENT_CFG, api, FakeSystem()) is True
    assert api.created == ["alice"]
    assert api.uploaded == []
    assert "documents down" in caplog.text


def test_restore_makes_unix_accounts_for_counted_entries_only():
    system = FakeSystem(
        accounts=[
            SambaAccount("alice", 20005, True),
            SambaAccount("desk", None, True),
            SambaAccount("low", 19999, True),
        ]
    )
    assert restore_unix_accounts(system, 20000) == 0
    assert system.calls == [("create_unix_user", "alice", 20005)]


def test_restore_continues_after_one_entry_fails():
    system = FakeSystem(
        accounts=[SambaAccount("alice", 20005, True), SambaAccount("bob", 20006, True)],
        fail_user="alice",
    )
    assert restore_unix_accounts(system, 20000) == 1
    assert ("create_unix_user", "bob", 20006) in system.calls


def test_daemon_touches_the_heartbeat_after_a_failed_cycle(monkeypatch, tmp_path):
    heartbeat = tmp_path / "heartbeat"

    def stop(_seconds):
        raise StopLoop()

    monkeypatch.setattr("smb_sync.service.time.sleep", stop)
    with pytest.raises(StopLoop):
        run_daemon(
            CFG,
            FakeApi(fail=True),
            FakeSystem(),
            lock_path=str(tmp_path / "lock"),
            heartbeat_path=str(heartbeat),
        )
    assert heartbeat.exists()


def test_daemon_drops_the_heartbeat_of_an_earlier_run(monkeypatch, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    seen = []

    class WatchingApi(FakeApi):
        def fetch_desired_users(self, group_name):
            seen.append(heartbeat.exists())
            return []

    def stop(_seconds):
        raise StopLoop()

    monkeypatch.setattr("smb_sync.service.time.sleep", stop)
    with pytest.raises(StopLoop):
        run_daemon(
            CFG,
            WatchingApi(),
            FakeSystem(),
            lock_path=str(tmp_path / "lock"),
            heartbeat_path=str(heartbeat),
        )
    assert seen == [False]


def test_heartbeat_goes_stale_after_three_intervals_or_three_minutes(tmp_path):
    path = tmp_path / "heartbeat"
    assert heartbeat_is_fresh(str(path), 100, now=1000.0) is False
    path.touch()
    os.utime(path, (1000.0, 1000.0))
    assert heartbeat_is_fresh(str(path), 100, now=1300.0) is True
    assert heartbeat_is_fresh(str(path), 100, now=1301.0) is False
    # one request to a paperless that hangs takes 30 s, so a short interval keeps a floor of 180 s
    assert heartbeat_is_fresh(str(path), 5, now=1180.0) is True
    assert heartbeat_is_fresh(str(path), 5, now=1181.0) is False
