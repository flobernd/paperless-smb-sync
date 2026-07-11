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
)
from smb_sync.system import CommandError


class FakeSystem:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def _record(self, name, *args):
        if name == self.fail_on:
            raise CommandError(f"{name} failed")
        self.calls.append((name, *args))

    def create_unix_user(self, username, uid):
        self._record("create_unix_user", username, uid)

    def delete_unix_user(self, username):
        self._record("delete_unix_user", username)

    def rename_unix_user(self, old, new):
        self._record("rename_unix_user", old, new)

    def set_samba_password(self, username, password, create=False):
        self._record("set_samba_password", username, password, create)

    def delete_samba_account(self, username):
        self._record("delete_samba_account", username)

    def disable_samba_account(self, username):
        self._record("disable_samba_account", username)

    def enable_samba_account(self, username):
        self._record("enable_samba_account", username)

    def rename_samba_account(self, old, new):
        self._record("rename_samba_account", old, new)

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


def run(actions, system=None, api=None):
    system = system or FakeSystem()
    api = api or FakeApi()
    failures = execute(actions, system, api, smb_host="nas.lan", share_name="inbox")
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
        ("rename_unix_user", "alice", "dave"),
        ("rename_samba_account", "alice", "dave"),
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
