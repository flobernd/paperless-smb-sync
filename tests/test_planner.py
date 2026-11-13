from dataclasses import replace

import pytest

from smb_sync.model import (
    ActualState,
    CreateAccount,
    CreateFolder,
    CreateWorkflow,
    CredentialDocument,
    DeleteDocument,
    DeleteWorkflow,
    Deprovision,
    DisableAccount,
    EnableAccount,
    HandOverDocument,
    InboxFolder,
    ManagedWorkflow,
    PaperlessUser,
    RenameDocument,
    RenameUser,
    RepairUnixAccount,
    SambaAccount,
    TokenUser,
    UpdateWorkflow,
)
from smb_sync.planner import plan_cycle

UID_BASE = 20000


def planned(desired, actual):
    return plan_cycle(desired, actual, UID_BASE)


def member(user_id, username, active=True):
    return PaperlessUser(user_id, username, active, 1731405600123, True)


def workflow(workflow_id, username, user_id):
    return ManagedWorkflow(workflow_id, username, user_id)


def empty_state():
    return ActualState(
        accounts=(),
        folders=(),
        workflows=(),
        unix_uids={},
        documents=(),
        token_user=None,
    )


def state(**fields):
    return replace(empty_state(), **fields)


def provisioned_state(username="alice", user_id=5, enabled=True, workflow_id=7):
    return state(
        accounts=(SambaAccount(username, UID_BASE + user_id, enabled),),
        folders=(InboxFolder(username, UID_BASE + user_id),),
        workflows=(workflow(workflow_id, username, user_id),),
        unix_uids={username: UID_BASE + user_id},
    )


def test_steady_state_no_actions():
    desired = [member(5, "alice")]
    plan = planned(desired, provisioned_state())
    assert plan.actions == ()


def test_a_workflow_defined_for_another_user_is_updated():
    # a person gave the workflow of alice to user 9, with the rights of user 9
    desired = [member(5, "alice")]
    actual = state(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(ManagedWorkflow(7, "alice", 9),),
        unix_uids={"alice": 20005},
    )
    assert planned(desired, actual).actions == (
        UpdateWorkflow(workflow_id=7, user_id=5, username="alice"),
    )


def test_inactive_user_disabled():
    desired = [member(5, "alice", active=False)]
    plan = planned(desired, provisioned_state(enabled=True))
    assert plan.actions == (DisableAccount(username="alice"),)


def test_reactivated_user_enabled():
    desired = [member(5, "alice")]
    plan = planned(desired, provisioned_state(enabled=False))
    assert plan.actions == (EnableAccount(username="alice"),)


def test_new_inactive_user_provisioned_disabled():
    desired = [member(5, "alice", active=False)]
    plan = planned(desired, empty_state())
    assert DisableAccount(username="alice") in plan.actions
    assert CreateAccount(user=desired[0], uid=20005, upload=False) in plan.actions


def test_missing_folder_and_workflow_repaired():
    desired = [member(5, "alice")]
    actual = state(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(),
        workflows=(),
        unix_uids={"alice": 20005},
    )
    plan = planned(desired, actual)
    assert plan.actions == (
        CreateFolder(username="alice", uid=20005),
        CreateWorkflow(user_id=5, username="alice"),
    )


def test_existing_folder_wrong_uid_is_adopted():
    # steady state: the folder is present under the right name but carries a
    # stale uid, so the planner corrects its owner for the account to write to it
    desired = [member(5, "alice")]
    actual = state(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 1000),),
        workflows=(workflow(7, "alice", 5),),
        unix_uids={"alice": 20005},
    )
    plan = planned(desired, actual)
    assert plan.actions == (CreateFolder(username="alice", uid=20005),)


def test_new_account_adopts_an_existing_folder_with_another_uid():
    # no account yet, but an inbox folder of that name exists under another
    # uid and must be adopted
    desired = [member(5, "alice")]
    actual = state(
        accounts=(),
        folders=(InboxFolder("alice", 1000),),
        workflows=(),
    )
    plan = planned(desired, actual)
    assert plan.actions == (
        CreateAccount(user=desired[0], uid=20005, upload=False),
        CreateFolder(username="alice", uid=20005),
        CreateWorkflow(user_id=5, username="alice"),
    )


def test_stale_account_keeps_a_folder_of_another_uid():
    actual = state(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20009),),
        workflows=(),
    )
    plan = planned([], actual)
    assert plan.actions == (
        Deprovision(
            username="alice",
            user_id=5,
            workflow_id=None,
            delete_folder=False,
            documents=None,
        ),
    )


def test_each_removal_candidate_goes_in_the_cycle_that_finds_it():
    # the removal of bob closed his login in an earlier cycle and then failed
    actual = state(
        accounts=(SambaAccount("alice", 20005, True), SambaAccount("bob", 20006, False)),
        folders=(InboxFolder("alice", 20005), InboxFolder("bob", 20006)),
        workflows=(workflow(7, "alice", 5), workflow(8, "bob", 6)),
    )
    assert planned([], actual).actions == (
        Deprovision(username="alice", user_id=5, workflow_id=7, delete_folder=True, documents=None),
        Deprovision(username="bob", user_id=6, workflow_id=8, delete_folder=True, documents=None),
    )


def test_workflow_without_account_and_user_is_deleted():
    actual = state(workflows=(workflow(8, "ghost", 9),))
    assert planned([], actual).actions == (DeleteWorkflow(workflow_id=8),)


def test_removal_of_an_old_name_keeps_a_folder_with_the_uid_of_a_user():
    desired = [member(5, "dave")]
    actual = state(
        accounts=(
            SambaAccount("alice", 20005, True),
            SambaAccount("dave", 20005, True),
        ),
        folders=(InboxFolder("alice", 20005),),
        workflows=(),
        unix_uids={"dave": 20005},
    )
    plan = planned(desired, actual)
    assert (
        Deprovision(
            username="alice",
            user_id=5,
            workflow_id=None,
            delete_folder=False,
            documents=None,
        )
        in plan.actions
    )


def test_rename_detected_by_uid():
    desired = [member(5, "dave")]
    plan = planned(desired, provisioned_state(username="alice"))
    assert plan.actions == (
        RenameUser(
            old_username="alice",
            new_username="dave",
            uid=20005,
            user_id=5,
            workflow_id=7,
        ),
    )


def test_folder_without_account_is_warned_not_deleted():
    actual = state(
        accounts=(), folders=(InboxFolder("archive", 65534),), workflows=()
    )
    plan = planned([], actual)
    assert plan.actions == ()
    assert any("archive" in w for w in plan.warnings)


def test_unmanaged_folder_warned_not_deleted():
    actual = state(
        accounts=(), folders=(InboxFolder("manual", 0),), workflows=()
    )
    plan = planned([], actual)
    assert plan.actions == ()
    assert any("manual" in w for w in plan.warnings)


def test_account_with_another_stored_uid_gets_no_action():
    # user 5 held the name alice, and a stopped removal disabled the entry
    desired = [member(9, "alice")]
    actual = state(
        accounts=(SambaAccount("alice", 20005, False),),
        folders=(InboxFolder("alice", 20005),),
        unix_uids={"alice": 20005},
    )
    plan = planned(desired, actual)
    assert plan.actions == ()
    (warning,) = plan.warnings
    assert "20005" in warning and "20009" in warning


def test_inactive_user_with_another_stored_uid_has_the_login_disabled():
    desired = [member(9, "alice", active=False)]
    actual = state(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20005),),
        unix_uids={"alice": 20005},
    )
    assert planned(desired, actual).actions == (DisableAccount(username="alice"),)


def test_invalid_rename_preserves_existing_resources():
    desired = [member(5, "alice@example.com")]
    plan = planned(desired, provisioned_state())
    assert plan.actions == ()
    assert any("alice@example.com" in w for w in plan.warnings)
    assert not any("folder" in w for w in plan.warnings)


def test_user_named_like_another_unix_account_is_skipped():
    desired = [member(5, "mail")]
    taken = state(accounts=(), folders=(), workflows=(), unix_uids={"mail": 8})
    plan = planned(desired, taken)
    assert plan.actions == ()
    assert any("mail" in w for w in plan.warnings)

    # the same name with the user's own uid is a creation that stopped early
    own = state(accounts=(), folders=(), workflows=(), unix_uids={"mail": 20005})
    plan = planned(desired, own)
    assert CreateAccount(user=desired[0], uid=20005, upload=False) in plan.actions


def test_rename_to_the_name_of_another_unix_account_changes_nothing():
    desired = [member(5, "mail")]
    actual = state(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(workflow(7, "alice", 9),),
        unix_uids={"alice": 20005, "mail": 8},
    )
    plan = planned(desired, actual)
    assert plan.actions == ()


@pytest.mark.parametrize(
    ("username", "unix_uids"),
    [
        ("Bad Name", {"alice": 20005}),
        ("mail", {"alice": 20005, "mail": 8}),
    ],
)
def test_skipped_inactive_user_has_the_entry_of_its_uid_disabled(username, unix_uids):
    desired = [member(5, username, active=False)]
    actual = replace(provisioned_state(), unix_uids=unix_uids)
    assert planned(desired, actual).actions == (DisableAccount(username="alice"),)


def test_skipped_inactive_user_has_each_entry_of_its_uid_disabled():
    # a rename from alice to dave that stopped before the old entry went
    desired = [member(5, "Bad Name", active=False)]
    actual = state(
        accounts=(SambaAccount("alice", 20005, True), SambaAccount("dave", 20005, True)),
    )
    plan = planned(desired, actual)
    assert plan.actions == (
        DisableAccount(username="alice"),
        DisableAccount(username="dave"),
    )


def test_rename_with_deactivation_disables_the_old_name_then_the_new_name():
    desired = [member(5, "dave", active=False)]
    plan = planned(desired, provisioned_state(username="alice", enabled=True))
    assert plan.actions == (
        DisableAccount(username="alice"),
        RenameUser(
            old_username="alice",
            new_username="dave",
            uid=20005,
            user_id=5,
            workflow_id=7,
        ),
        DisableAccount(username="dave"),
    )


def test_stale_folder_with_desired_uid_frozen_with_warning():
    desired = [member(5, "dave")]
    actual = state(
        accounts=(), folders=(InboxFolder("alice", 20005),), workflows=()
    )
    plan = planned(desired, actual)
    assert any("alice" in w and "dave" in w for w in plan.warnings)


def test_stored_uid_below_the_range_does_not_count():
    # 19999 is the last uid outside the range, 20000 the first inside
    actual = state(
        accounts=(SambaAccount("below", 19999, True), SambaAccount("edge", 20000, True)),
        folders=(),
        workflows=(),
    )
    plan = planned([], actual)
    assert plan.actions == (
        Deprovision(
            username="edge",
            user_id=0,
            workflow_id=None,
            delete_folder=False,
            documents=None,
        ),
    )
    assert any("below" in w for w in plan.warnings)


def test_entry_without_stored_uid_is_created_again():
    desired = [member(5, "alice")]
    actual = state(
        accounts=(SambaAccount("alice", None, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(workflow(7, "alice", 5),),
        unix_uids={"alice": 20005},
    )
    plan = planned(desired, actual)
    assert plan.actions == (CreateAccount(user=desired[0], uid=20005, upload=False),)


def test_entry_without_stored_uid_and_without_user_is_left():
    actual = state(
        accounts=(SambaAccount("ghost", None, True),), folders=(), workflows=()
    )
    plan = planned([], actual)
    assert plan.actions == ()
    assert any("ghost" in w for w in plan.warnings)


def test_missing_unix_account_is_repaired_from_the_stored_uid():
    desired = [member(5, "alice")]
    actual = state(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(workflow(7, "alice", 5),),
    )
    plan = planned(desired, actual)
    assert plan.actions == (RepairUnixAccount(username="alice", uid=20005),)


def test_rename_with_reactivation_enables_the_old_name_then_the_new_name():
    desired = [member(5, "dave")]
    plan = planned(desired, provisioned_state(username="alice", enabled=False))
    assert plan.actions == (
        EnableAccount(username="alice"),
        RenameUser(
            old_username="alice",
            new_username="dave",
            uid=20005,
            user_id=5,
            workflow_id=7,
        ),
        EnableAccount(username="dave"),
    )


ADMIN = TokenUser(1, True)


def test_pending_document_goes_to_the_user_with_its_id_and_keeps_its_name():
    # uploaded for user 5 under the name alice, who is dave now, while user 9 took the name alice
    desired = [member(5, "dave"), member(9, "alice")]
    actual = state(
        accounts=(SambaAccount("alice", 20009, True), SambaAccount("dave", 20005, True)),
        folders=(InboxFolder("alice", 20009), InboxFolder("dave", 20005)),
        workflows=(workflow(8, "alice", 9), workflow(7, "dave", 5)),
        unix_uids={"alice": 20009, "dave": 20005},
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 1, recipient_id=5),),
    )
    assert planned(desired, actual).actions == (
        HandOverDocument(document_id=40, user_id=5, username="alice"),
    )


def test_rename_retitles_the_document_of_the_user():
    desired = [member(5, "dave")]
    actual = replace(
        provisioned_state(),
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 5, recipient_id=None),),
    )
    assert (
        RenameDocument(document_id=40, old_username="alice", new_username="dave")
        in planned(desired, actual).actions
    )


def test_document_that_carries_the_name_of_its_owner_needs_no_action():
    desired = [member(5, "alice")]
    actual = replace(
        provisioned_state(),
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 5, recipient_id=None),),
    )
    assert planned(desired, actual).actions == ()


def test_document_without_owner_or_account_is_deleted():
    actual = state(
        accounts=(),
        folders=(),
        workflows=(),
        token_user=ADMIN,
        documents=(
            CredentialDocument(40, "alice", None, recipient_id=None),
            CredentialDocument(41, "bob", 1, recipient_id=6),
            CredentialDocument(42, "carol", 9, recipient_id=None),
        ),
    )
    assert planned([], actual).actions == (
        DeleteDocument(document_id=40),
        DeleteDocument(document_id=41),
    )


def test_removal_takes_the_documents_of_that_account_only():
    actual = replace(
        provisioned_state(),
        accounts=(SambaAccount("alice", 20005, True), SambaAccount("bob", 20006, True)),
        token_user=ADMIN,
        documents=(
            CredentialDocument(40, "alice", 5, recipient_id=None),
            CredentialDocument(41, "bob", 6, recipient_id=None),
            CredentialDocument(42, "bob", 1, recipient_id=6),
            CredentialDocument(43, "alice", None, recipient_id=None),
        ),
    )
    assert planned([], actual).actions == (
        Deprovision(username="alice", user_id=5, workflow_id=7, delete_folder=True, documents=(40, 43)),
        Deprovision(username="bob", user_id=6, workflow_id=None, delete_folder=False, documents=(41, 42)),
    )


def test_removal_of_an_old_name_leaves_the_documents_of_the_renamed_user():
    # a rename from alice to dave that stopped before the old entry went
    desired = [member(5, "dave")]
    actual = state(
        accounts=(SambaAccount("alice", 20005, True), SambaAccount("dave", 20005, True)),
        folders=(InboxFolder("dave", 20005),),
        workflows=(workflow(7, "dave", 5),),
        unix_uids={"dave": 20005},
        token_user=ADMIN,
        documents=(CredentialDocument(40, "dave", 5, recipient_id=None),),
    )
    assert planned(desired, actual).actions == (
        Deprovision(
            username="alice",
            user_id=5,
            workflow_id=None,
            delete_folder=False,
            documents=None,
        ),
    )


def test_removal_of_the_token_user_leaves_the_uploads_for_other_users():
    # the user of the token is in the sync group and leaves it
    desired = [member(6, "bob")]
    actual = state(
        accounts=(SambaAccount("admin", 20001, True), SambaAccount("bob", 20006, True)),
        folders=(InboxFolder("bob", 20006),),
        workflows=(workflow(8, "bob", 6),),
        unix_uids={"admin": 20001, "bob": 20006},
        token_user=ADMIN,
        documents=(
            CredentialDocument(40, "admin", 1, recipient_id=None),
            CredentialDocument(41, "bob", 1, recipient_id=6),
        ),
    )
    assert planned(desired, actual).actions == (
        Deprovision(
            username="admin",
            user_id=1,
            workflow_id=None,
            delete_folder=False,
            documents=(40,),
        ),
        HandOverDocument(document_id=41, user_id=6, username="bob"),
    )


def test_pending_document_of_another_owner_is_left_with_a_warning():
    desired = [member(5, "alice")]
    actual = replace(
        provisioned_state(),
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 9, recipient_id=5),),
    )
    plan = planned(desired, actual)
    assert plan.actions == ()
    assert any("40" in w for w in plan.warnings)
