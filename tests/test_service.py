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
    check_volume,
    heartbeat_is_fresh,
    restore_unix_accounts,
    run_cycle,
    run_cycle_locked,
    run_daemon,
)
from smb_sync.system import CommandError, SystemAdapter

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


def member(user_id, username, active=True):
    return PaperlessUser(user_id, username, active, 1731405600123, True)


class FakeApi:
    def __init__(self, users=None, fail=False, token_user=None):
        self.users = users or []
        self.fail = fail
        self.created = []
        self._token_user = token_user
        self.uploaded = []

    def fetch_users(self, group_name):
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

    def list_unix_names(self):
        return frozenset()

    def list_session_accounts(self):
        return frozenset()

    def read_usermap(self):
        return None

    def write_usermap(self, lines):
        self.calls.append(("write_usermap", lines))

    def create_unix_user(self, username, uid):
        if username == self.fail_user:
            raise CommandError(f"uid {uid} is taken")
        self.calls.append(("create_unix_user", username, uid))

    def set_samba_password(self, username, password, create=False):
        self.calls.append(("set_samba_password", username))

    def store_mark(self, account, uid, joined):
        self.calls.append(("store_mark", account, uid, joined))

    def disable_samba_account(self, account):
        self.calls.append(("disable_samba_account", account))

    def create_inbox_folder(self, username, uid):
        self.calls.append(("create_inbox_folder", username, uid))


def test_api_failure_skips_cycle_without_touching_system():
    api = FakeApi(fail=True)
    system = FakeSystem()
    assert run_cycle(CFG, api, system) is False
    assert system.calls == []


def test_local_state_failure_skips_cycle():
    api = FakeApi(users=[member(5, "alice")])
    system = FakeSystem(fail_listing=True)
    assert run_cycle(CFG, api, system) is False
    assert system.calls == []


def test_happy_path_provisions(capsys):
    api = FakeApi(users=[member(5, "alice")])
    system = FakeSystem()
    assert run_cycle(CFG, api, system) is True
    assert ("create_unix_user", "u20005", 20005) in system.calls
    assert api.created == ["u20005"]


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
        def fetch_users(self, group_name):
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
        def fetch_users(self, group_name):
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
        [member(5, "Bad Name")],
        [member(5, "Bad Name"), member(6, "Other Bad")],
    ]

    class ChangingApi(FakeApi):
        def fetch_users(self, group_name):
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
    api = FakeApi(users=[member(5, "Bad Name")])
    with caplog.at_level(logging.WARNING):
        run_cycle(CFG, api, FakeSystem())
    assert "'Bad Name'" in caplog.text


DOCUMENT_CFG = replace(CFG, credentials_document=True)


def test_only_a_superuser_token_uploads_a_document(caplog):
    users = [member(5, "alice")]
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
        users=[member(5, "alice")], token_user=TokenUser(1, True)
    )
    with caplog.at_level(logging.ERROR):
        assert run_cycle(DOCUMENT_CFG, api, FakeSystem()) is True
    assert api.created == ["u20005"]
    assert api.uploaded == []
    assert "documents down" in caplog.text


def test_a_failed_session_read_does_not_stop_the_cycle(caplog):
    class NoSessionList(FakeSystem):
        def list_session_accounts(self):
            raise CommandError("smbstatus broken")

    system = NoSessionList()
    with caplog.at_level(logging.ERROR):
        run_cycle(CFG, FakeApi(users=[member(5, "alice")]), system)
    assert ("create_unix_user", "u20005", 20005) in system.calls
    assert "smbstatus broken" in caplog.text


def test_restore_makes_unix_accounts_for_counted_entries_only():
    system = FakeSystem(
        accounts=[
            SambaAccount("u20005", "smb-sync uid=20005 joined=1731405600123", True),
            SambaAccount("desk", "", True),
            SambaAccount("u19999", "smb-sync uid=19999 joined=1731405600123", True),
        ]
    )
    assert restore_unix_accounts(system, 20000) == 0
    assert system.calls == [("create_unix_user", "u20005", 20005)]


def test_restore_continues_after_one_entry_fails():
    system = FakeSystem(
        accounts=[
            SambaAccount("u20005", "smb-sync uid=20005 joined=1731405600123", True),
            SambaAccount("u20006", "smb-sync uid=20006 joined=1731405600123", True),
        ],
        fail_user="u20005",
    )
    assert restore_unix_accounts(system, 20000) == 1
    assert ("create_unix_user", "u20006", 20006) in system.calls


OPERATOR_ENTRY = """\
---------------
Unix username:        desk
Account Flags:        [U          ]
Account desc:         Front desk scanner: do not delete
"""


def test_a_first_start_writes_the_base_and_a_map_with_the_last_line(tmp_path):
    system = SystemAdapter(
        str(tmp_path),
        10000,
        run=lambda argv, input_text=None: OPERATOR_ENTRY,
        state_dir=str(tmp_path),
    )
    assert check_volume(system, 20000) is None
    assert system.read_uid_base() == "20000"
    assert system.read_usermap() == ("!nobody = *",)
    # the next start finds the files of this one
    assert check_volume(system, 20000) is None


def test_daemon_drops_the_heartbeat_of_an_earlier_run(monkeypatch, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    seen = []

    class WatchingApi(FakeApi):
        def fetch_users(self, group_name):
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
