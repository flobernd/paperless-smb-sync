import logging

import pytest

from smb_sync.executor import execute
from smb_sync.model import (
    CreateAccount,
    CreateFolder,
    CreateWorkflow,
    DeleteWorkflow,
    Deprovision,
    DisableAccount,
    EnableAccount,
    PaperlessUser,
    RenameDocument,
    RenameUser,
    RepairUnixAccount,
    UpdateWorkflow,
)
from smb_sync.paperless import PaperlessError
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

    def check_samba_rename(self, old, new):
        self._record("check_samba_rename", old, new)

    def rename_samba_account(self, old, new, uid):
        self._record("rename_samba_account", old, new, uid)

    def store_uid(self, username, uid):
        self._record("store_uid", username, uid)

    def create_inbox_folder(self, username, uid):
        self._record("create_inbox_folder", username, uid)

    def delete_inbox_folder(self, username):
        self._record("delete_inbox_folder", username)

    def rename_inbox_folder(self, old, new):
        self._record("rename_inbox_folder", old, new)


class FakeApi:
    def __init__(self, fail_upload=False):
        self.calls = []
        self.blocks = []
        self.fail_upload = fail_upload

    def upload_credentials(self, user_id, username, block):
        if self.fail_upload:
            raise PaperlessError("upload refused")
        self.calls.append(("upload_credentials", user_id, username))
        self.blocks.append(block)

    def create_workflow(self, user_id, username):
        self.calls.append(("create_workflow", user_id, username))

    def delete_workflow(self, workflow_id):
        self.calls.append(("delete_workflow", workflow_id))

    def update_workflow(self, workflow_id, user_id, username):
        self.calls.append(("update_workflow", workflow_id, user_id, username))

    def ensure_document_note(self, document_id, note):
        self.calls.append(("ensure_document_note", document_id))


def run(actions, system=None, api=None):
    system = system or FakeSystem()
    api = api or FakeApi()
    failures = execute(
        actions,
        system,
        api,
        smb_host="nas.lan",
        share_name="inbox",
    )
    return failures, system, api


def member(user_id, username, active=True):
    return PaperlessUser(user_id, username, active, 1731405600123, True)


def test_create_account_sets_password_and_prints_once(capsys):
    user = member(5, "alice")
    failures, system, _ = run([CreateAccount(user=user, uid=20005, upload=False)])
    assert failures == 0
    assert system.calls[0] == ("create_unix_user", "alice", 20005)
    name, username, password, create = system.calls[1]
    assert (name, username, create) == ("set_samba_password", "alice", True)
    out = capsys.readouterr().out
    assert "SMB credentials for 'alice'" in out
    assert password in out
    assert "\\\\nas.lan\\inbox" in out
    assert system.calls[-1] == ("store_uid", "alice", 20005)


def test_a_creation_whose_uid_write_fails_hands_out_no_password(capsys):
    user = member(5, "alice")
    failures, system, api = run(
        [CreateAccount(user=user, uid=20005, upload=True)],
        system=FakeSystem(fail_on="store_uid"),
    )
    assert failures == 1
    _, _, password, _ = system.calls[1]
    assert password not in capsys.readouterr().out
    assert api.calls == []


def test_folder_workflow_and_state_actions():
    failures, system, api = run(
        [
            CreateFolder(username="alice", uid=20005),
            CreateWorkflow(user_id=5, username="alice"),
            UpdateWorkflow(workflow_id=7, user_id=5, username="alice"),
            DisableAccount(username="alice"),
            EnableAccount(username="alice"),
            DeleteWorkflow(workflow_id=7),
        ]
    )
    assert failures == 0
    assert ("create_inbox_folder", "alice", 20005) in system.calls
    assert ("disable_samba_account", "alice") in system.calls
    assert ("enable_samba_account", "alice") in system.calls
    assert ("create_workflow", 5, "alice") in api.calls
    assert ("update_workflow", 7, 5, "alice") in api.calls
    assert ("delete_workflow", 7) in api.calls


def test_repair_unix_account_recreates_unix_user_only():
    failures, system, api = run([RepairUnixAccount(username="alice", uid=20005)])
    assert failures == 0
    assert system.calls == [("create_unix_user", "alice", 20005)]
    assert api.calls == []


def test_removal_stops_at_the_first_failed_step():
    system = FakeSystem(fail_on="delete_inbox_folder")
    failures, system, api = run(
        [
            Deprovision(
                username="alice",
                user_id=5,
                workflow_id=7,
                delete_folder=True,
                documents=None,
            )
        ],
        system=system,
    )
    assert failures == 1
    assert system.calls == [("disable_samba_account", "alice")]
    assert api.calls == [("delete_workflow", 7)]


def test_removal_without_the_folder_flag_leaves_the_folder():
    failures, system, api = run(
        [
            Deprovision(
                username="alice",
                user_id=5,
                workflow_id=None,
                delete_folder=False,
                documents=None,
            )
        ]
    )
    assert failures == 0
    assert system.calls == [
        ("disable_samba_account", "alice"),
        ("delete_unix_user", "alice"),
        ("delete_samba_account", "alice"),
    ]
    assert api.calls == []


def test_removal_closes_the_login_before_the_workflow_step():
    api = FakeApi()

    def refuse(workflow_id):
        raise PaperlessError("HTTP 500")

    api.delete_workflow = refuse
    failures, system, _ = run(
        [
            Deprovision(
                username="alice",
                user_id=5,
                workflow_id=7,
                delete_folder=True,
                documents=None,
            )
        ],
        api=api,
    )
    assert failures == 1
    assert system.calls == [("disable_samba_account", "alice")]


def test_a_failed_document_purge_keeps_the_entry():
    # while the entry exists, the next cycle tries the removal again
    api = FakeApi()

    def refuse(username, user_id, document_ids):
        raise PaperlessError("HTTP 500")

    api.delete_credentials = refuse
    failures, system, _ = run(
        [
            Deprovision(
                username="alice",
                user_id=5,
                workflow_id=None,
                delete_folder=True,
                documents=(40,),
            )
        ],
        api=api,
    )
    assert failures == 1
    assert ("delete_samba_account", "alice") not in system.calls


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
        ("check_samba_rename", "alice", "dave"),
        ("rename_inbox_folder", "alice", "dave"),
        ("rename_samba_account", "alice", "dave", 20005),
    ]
    assert api.calls == [("update_workflow", 7, 5, "dave")]


def test_a_refused_rename_moves_no_folder():
    failures, system, api = run(
        [
            RenameUser(
                old_username="alice",
                new_username="dave",
                uid=20005,
                user_id=5,
                workflow_id=7,
            )
        ],
        system=FakeSystem(fail_on="check_samba_rename"),
    )
    assert failures == 1
    assert system.calls == []
    assert api.calls == []


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


def test_a_document_rename_adds_the_note_before_the_title():
    api = FakeApi()

    def refuse(document_id, username):
        raise PaperlessError("HTTP 500")

    api.retitle_document = refuse
    failures, _, api = run(
        [RenameDocument(document_id=40, old_username="alice", new_username="dave")],
        api=api,
    )
    assert failures == 1
    assert api.calls == [("ensure_document_note", 40)]


def test_unexpected_exception_is_isolated_and_counted():
    system = FakeSystem(fail_on="disable_samba_account", fail_with=RuntimeError)
    failures, system, _ = run(
        [
            DisableAccount(username="alice"),
            CreateFolder(username="bob", uid=20006),
        ],
        system=system,
    )
    assert failures == 1
    assert ("create_inbox_folder", "bob", 20006) in system.calls


def test_unexpected_failure_logs_traceback(caplog):
    system = FakeSystem(fail_on="disable_samba_account", fail_with=RuntimeError)
    with caplog.at_level(logging.ERROR):
        run([DisableAccount(username="alice")], system=system)
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("error", [CommandError, PaperlessError])
def test_a_failed_command_or_request_logs_one_line_without_a_traceback(error, caplog):
    system = FakeSystem(fail_on="disable_samba_account", fail_with=error)
    with caplog.at_level(logging.ERROR):
        failures, _, _ = run([DisableAccount(username="alice")], system=system)
    assert failures == 1
    (record,) = caplog.records
    assert not record.exc_info


def test_a_failed_upload_does_not_fail_the_creation(caplog):
    user = member(5, "alice")
    with caplog.at_level(logging.ERROR):
        failures, system, _ = run(
            [CreateAccount(user=user, uid=20005, upload=True)],
            api=FakeApi(fail_upload=True),
        )
    assert failures == 0
    assert system.calls[-1] == ("store_uid", "alice", 20005)
    assert "upload refused" in caplog.text


def test_the_uploaded_block_drops_the_lines_for_the_log(capsys):
    api = FakeApi()
    user = member(5, "alice")
    run([CreateAccount(user=user, uid=20005, upload=True)], api=api)
    (block,) = api.blocks
    assert "Username: alice" in block
    assert "smb-sync regen" not in block
    # the log keeps the hint, since that is where an admin can act on it
    assert "smb-sync regen alice" in capsys.readouterr().out
