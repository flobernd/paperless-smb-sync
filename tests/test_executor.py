import logging

from smb_sync.credentials import PASSWORD_LENGTH, generate_password
from smb_sync.executor import execute
from smb_sync.model import (
    CreateAccount,
    CreateFolder,
    CreateWorkflow,
    DeleteAccount,
    DeleteFolder,
    DeleteWorkflow,
    DisableAccount,
    EnableAccount,
    PaperlessUser,
    RenameUser,
    RepairUnixAccount,
)
from smb_sync.mail import MailError
from smb_sync.system import CommandError


class FakeSystem:
    def __init__(self, fail_on=None, fail_with=CommandError):
        self.calls = []
        self.fail_on = fail_on
        self.fail_with = fail_with

    def _record(self, name, *args):
        if name == self.fail_on:
            raise self.fail_with(f"{name} failed")
        self.calls.append((name, *args))

    def create_unix_user(self, username, uid):
        self._record("create_unix_user", username, uid)

    def delete_unix_user(self, username):
        self._record("delete_unix_user", username)

    def set_samba_password(self, username, password, create=False):
        self._record("set_samba_password", username, password, create)

    def delete_samba_account(self, username):
        self._record("delete_samba_account", username)

    def disable_samba_account(self, username):
        self._record("disable_samba_account", username)

    def enable_samba_account(self, username):
        self._record("enable_samba_account", username)

    def rename_samba_account(self, old, new, uid):
        self._record("rename_samba_account", old, new, uid)

    def create_inbox_folder(self, username, uid):
        self._record("create_inbox_folder", username, uid)

    def delete_inbox_folder(self, username):
        self._record("delete_inbox_folder", username)

    def rename_inbox_folder(self, old, new):
        self._record("rename_inbox_folder", old, new)


class FakeApi:
    def __init__(self):
        self.calls = []

    def create_workflow(self, user_id, username):
        self.calls.append(("create_workflow", user_id, username))

    def delete_workflow(self, workflow_id):
        self.calls.append(("delete_workflow", workflow_id))

    def rename_workflow(self, workflow_id, user_id, new_username):
        self.calls.append(("rename_workflow", workflow_id, user_id, new_username))


class FakeDelivery:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def __call__(self, to, username, block):
        if self.fail:
            raise MailError("smtp down")
        self.sent.append((to, username, block))


def run(actions, system=None, api=None, deliver=None):
    system = system or FakeSystem()
    api = api or FakeApi()
    failures = execute(
        actions,
        system,
        api,
        smb_host="nas.lan",
        share_name="inbox",
        deliver=deliver,
    )
    return failures, system, api


def test_generate_password_shape():
    password = generate_password()
    assert len(password) == PASSWORD_LENGTH
    assert password.isalnum()


def test_create_account_sets_password_and_prints_once(capsys):
    user = PaperlessUser(5, "alice", True)
    failures, system, _ = run([CreateAccount(user=user, uid=20005)])
    assert failures == 0
    assert system.calls[0] == ("create_unix_user", "alice", 20005)
    name, username, password, create = system.calls[1]
    assert (name, username, create) == ("set_samba_password", "alice", True)
    out = capsys.readouterr().out
    assert "SMB credentials for 'alice'" in out
    assert password in out
    assert "\\\\nas.lan\\inbox" in out


def test_folder_workflow_and_state_actions():
    failures, system, api = run(
        [
            CreateFolder(username="alice", uid=20005),
            CreateWorkflow(user_id=5, username="alice"),
            DisableAccount(username="alice"),
            EnableAccount(username="alice"),
            DeleteWorkflow(workflow_id=7),
            DeleteFolder(username="alice"),
        ]
    )
    assert failures == 0
    assert ("create_inbox_folder", "alice", 20005) in system.calls
    assert ("disable_samba_account", "alice") in system.calls
    assert ("enable_samba_account", "alice") in system.calls
    assert ("delete_inbox_folder", "alice") in system.calls
    assert ("create_workflow", 5, "alice") in api.calls
    assert ("delete_workflow", 7) in api.calls


def test_repair_unix_account_recreates_unix_user_only():
    failures, system, api = run([RepairUnixAccount(username="alice", uid=20005)])
    assert failures == 0
    assert system.calls == [("create_unix_user", "alice", 20005)]
    assert api.calls == []


def test_delete_account_removes_samba_before_unix():
    failures, system, _ = run([DeleteAccount(username="alice")])
    assert failures == 0
    assert system.calls == [
        ("delete_samba_account", "alice"),
        ("delete_unix_user", "alice"),
    ]


def test_rename_sequence_and_workflow_update():
    failures, system, api = run(
        [
            RenameUser(
                old_username="alice",
                new_username="dave",
                uid=20005,
                user_id=5,
                workflow_id=7,
            )
        ]
    )
    assert failures == 0
    assert system.calls == [
        ("rename_samba_account", "alice", "dave", 20005),
        ("rename_inbox_folder", "alice", "dave"),
    ]
    assert api.calls == [("rename_workflow", 7, 5, "dave")]


def test_rename_without_workflow_skips_api():
    failures, _, api = run(
        [
            RenameUser(
                old_username="alice",
                new_username="dave",
                uid=20005,
                user_id=5,
                workflow_id=None,
            )
        ]
    )
    assert failures == 0
    assert api.calls == []


def test_failure_is_isolated_and_counted():
    system = FakeSystem(fail_on="disable_samba_account")
    failures, system, _ = run(
        [
            DisableAccount(username="alice"),
            DeleteFolder(username="bob"),
        ],
        system=system,
    )
    assert failures == 1
    assert ("delete_inbox_folder", "bob") in system.calls


def test_unexpected_exception_is_isolated_and_counted():
    system = FakeSystem(fail_on="disable_samba_account", fail_with=RuntimeError)
    failures, system, _ = run(
        [
            DisableAccount(username="alice"),
            DeleteFolder(username="bob"),
        ],
        system=system,
    )
    assert failures == 1
    assert ("delete_inbox_folder", "bob") in system.calls


def test_unexpected_failure_logs_traceback(caplog):
    system = FakeSystem(fail_on="disable_samba_account", fail_with=RuntimeError)
    with caplog.at_level(logging.ERROR):
        run([DisableAccount(username="alice")], system=system)
    assert any(record.exc_info for record in caplog.records)


def test_multiple_failures_accumulate():
    system = FakeSystem(fail_on="disable_samba_account")
    failures, system, _ = run(
        [
            DisableAccount(username="alice"),
            DisableAccount(username="bob"),
            DeleteFolder(username="carol"),
        ],
        system=system,
    )
    assert failures == 2
    assert ("delete_inbox_folder", "carol") in system.calls


def test_emails_the_credentials_when_delivery_is_configured(capsys):
    deliver = FakeDelivery()
    user = PaperlessUser(5, "alice", True, "alice@example.com")
    failures, system, _ = run([CreateAccount(user=user, uid=20005)], deliver=deliver)
    assert failures == 0
    to, username, block = deliver.sent[0]
    assert (to, username) == ("alice@example.com", "alice")
    password = system.calls[1][2]
    assert password in block
    # the printed block stays as the fallback for a mail that never arrives
    assert password in capsys.readouterr().out


def test_warns_when_the_user_has_no_email_address(caplog):
    deliver = FakeDelivery()
    user = PaperlessUser(5, "alice", True, "")
    with caplog.at_level(logging.WARNING):
        failures, _, _ = run([CreateAccount(user=user, uid=20005)], deliver=deliver)
    assert failures == 0
    assert deliver.sent == []
    assert "no email address" in caplog.text


def test_a_failed_mail_does_not_fail_provisioning(caplog):
    deliver = FakeDelivery(fail=True)
    user = PaperlessUser(5, "alice", True, "alice@example.com")
    with caplog.at_level(logging.ERROR):
        failures, system, _ = run([CreateAccount(user=user, uid=20005)], deliver=deliver)
    assert failures == 0
    assert [call[0] for call in system.calls] == [
        "create_unix_user",
        "set_samba_password",
    ]
    assert "smtp down" in caplog.text


def test_the_emailed_block_drops_the_admin_only_regen_hint(capsys):
    deliver = FakeDelivery()
    user = PaperlessUser(5, "alice", True, "alice@example.com")
    run([CreateAccount(user=user, uid=20005)], deliver=deliver)
    _, _, block = deliver.sent[0]
    assert "smb-sync regen" not in block
    # the log keeps it, since that is where an admin can act on it
    assert "smb-sync regen alice" in capsys.readouterr().out
