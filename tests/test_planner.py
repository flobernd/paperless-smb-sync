from dataclasses import replace

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
CONSUME = "/usr/src/paperless/consume"


def planned(desired, actual):
    return plan_cycle(desired, actual, UID_BASE, CONSUME)


def workflow(workflow_id, username):
    return ManagedWorkflow(workflow_id, username, f"{CONSUME}/{username}/*")


def empty_state():
    return ActualState(accounts=(), folders=(), workflows=())


def provisioned_state(username="alice", user_id=5, enabled=True, workflow_id=7):
    return ActualState(
        accounts=(SambaAccount(username, UID_BASE + user_id, enabled),),
        folders=(InboxFolder(username, UID_BASE + user_id),),
        workflows=(workflow(workflow_id, username),),
        unix_uids={username: UID_BASE + user_id},
    )


def test_steady_state_no_actions():
    desired = [PaperlessUser(5, "alice", True)]
    plan = planned(desired, provisioned_state())
    assert plan.actions == ()


def test_workflow_with_another_filter_is_updated():
    desired = [PaperlessUser(5, "alice", True)]
    actual = ActualState(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(ManagedWorkflow(7, "alice", "*/alice/*"),),
        unix_uids={"alice": 20005},
    )
    plan = plan_cycle(desired, actual, UID_BASE, "/usr/src/paperless/consume")
    assert plan.actions == (UpdateWorkflow(workflow_id=7, user_id=5, username="alice"),)


def test_inactive_user_disabled():
    desired = [PaperlessUser(5, "alice", False)]
    plan = planned(desired, provisioned_state(enabled=True))
    assert plan.actions == (DisableAccount(username="alice"),)


def test_reactivated_user_enabled():
    desired = [PaperlessUser(5, "alice", True)]
    plan = planned(desired, provisioned_state(enabled=False))
    assert plan.actions == (EnableAccount(username="alice"),)


def test_new_inactive_user_provisioned_disabled():
    desired = [PaperlessUser(5, "alice", False)]
    plan = planned(desired, empty_state())
    assert DisableAccount(username="alice") in plan.actions
    assert CreateAccount(user=desired[0], uid=20005) in plan.actions


def test_missing_folder_and_workflow_repaired():
    desired = [PaperlessUser(5, "alice", True)]
    actual = ActualState(
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
    # stale uid; correct its ownership so the new account can write to it
    desired = [PaperlessUser(5, "alice", True)]
    actual = ActualState(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 1000),),
        workflows=(workflow(7, "alice"),),
        unix_uids={"alice": 20005},
    )
    plan = planned(desired, actual)
    assert plan.actions == (CreateFolder(username="alice", uid=20005),)


def test_migration_adopts_prexisting_folder_wrong_uid():
    # first sync after migration: no account yet, but the legacy inbox
    # folder already exists under a non-managed uid and must be adopted
    desired = [PaperlessUser(5, "alice", True)]
    actual = ActualState(
        accounts=(),
        folders=(InboxFolder("alice", 1000),),
        workflows=(),
    )
    plan = planned(desired, actual)
    assert plan.actions == (
        CreateAccount(user=desired[0], uid=20005),
        CreateFolder(username="alice", uid=20005),
        CreateWorkflow(user_id=5, username="alice"),
    )


def test_removal_takes_the_workflow_and_the_folder_of_the_account():
    plan = planned([], provisioned_state())
    assert plan.actions == (
        Deprovision(username="alice", workflow_id=7, delete_folder=True),
    )


def test_stale_account_keeps_a_folder_of_another_uid():
    actual = ActualState(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20009),),
        workflows=(),
    )
    plan = planned([], actual)
    assert plan.actions == (
        Deprovision(username="alice", workflow_id=None, delete_folder=False),
    )


def test_stale_folder_beside_an_entry_that_does_not_count_deletes_nothing():
    actual = ActualState(
        accounts=(SambaAccount("bob", None, True),),
        folders=(InboxFolder("bob", 20006),),
        workflows=(),
    )
    plan = planned([], actual)
    assert plan.actions == ()


def test_at_most_one_deprovision_per_cycle():
    actual = ActualState(
        accounts=(
            SambaAccount("alice", 20005, True),
            SambaAccount("bob", 20006, True),
        ),
        folders=(InboxFolder("alice", 20005), InboxFolder("bob", 20006)),
        workflows=(workflow(7, "alice"), workflow(8, "bob")),
    )
    plan = planned([], actual)
    removals = [a for a in plan.actions if isinstance(a, Deprovision)]
    assert removals == [
        Deprovision(username="alice", workflow_id=7, delete_folder=True)
    ]
    assert any("bob" in w and "deferred" in w for w in plan.warnings)
    # bob's workflow removal is non-destructive and not deferred
    assert DeleteWorkflow(workflow_id=8) in plan.actions


def test_removal_of_an_old_name_keeps_a_folder_with_the_uid_of_a_user():
    desired = [PaperlessUser(5, "dave", True)]
    actual = ActualState(
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
        Deprovision(username="alice", workflow_id=None, delete_folder=False)
        in plan.actions
    )


def test_a_removal_that_stopped_does_not_hold_back_an_open_login():
    # the removal of alice closed her login in an earlier cycle and then failed
    actual = ActualState(
        accounts=(
            SambaAccount("alice", 20005, False),
            SambaAccount("bob", 20006, True),
        ),
        folders=(),
        workflows=(),
    )
    plan = planned([], actual)
    assert plan.actions == (
        Deprovision(username="bob", workflow_id=None, delete_folder=False),
    )
    assert any("alice" in w for w in plan.warnings)


def test_unmanaged_account_untouched():
    actual = ActualState(
        accounts=(SambaAccount("legacy", 1000, True),),
        folders=(),
        workflows=(),
    )
    plan = planned([], actual)
    assert plan.actions == ()


def test_rename_detected_by_uid():
    desired = [PaperlessUser(5, "dave", True)]
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
    actual = ActualState(
        accounts=(), folders=(InboxFolder("archive", 65534),), workflows=()
    )
    plan = planned([], actual)
    assert plan.actions == ()
    assert any("archive" in w for w in plan.warnings)


def test_unmanaged_folder_warned_not_deleted():
    actual = ActualState(
        accounts=(), folders=(InboxFolder("manual", 0),), workflows=()
    )
    plan = planned([], actual)
    assert plan.actions == ()
    assert any("manual" in w for w in plan.warnings)


def test_uid_mismatch_warned_not_touched():
    desired = [PaperlessUser(6, "alice", True)]
    plan = planned(desired, provisioned_state(username="alice", user_id=5))
    assert plan.actions == ()
    assert any("uid" in w.lower() for w in plan.warnings)


def test_invalid_rename_preserves_existing_resources():
    desired = [PaperlessUser(5, "alice@example.com", True)]
    plan = planned(desired, provisioned_state())
    assert plan.actions == ()
    assert any("alice@example.com" in w for w in plan.warnings)
    assert not any("folder" in w for w in plan.warnings)


def test_user_named_like_another_unix_account_is_skipped():
    desired = [PaperlessUser(5, "mail", True)]
    taken = ActualState(accounts=(), folders=(), workflows=(), unix_uids={"mail": 8})
    plan = planned(desired, taken)
    assert plan.actions == ()
    assert any("mail" in w for w in plan.warnings)

    # the same name with the user's own uid is a creation that stopped early
    own = ActualState(accounts=(), folders=(), workflows=(), unix_uids={"mail": 20005})
    plan = planned(desired, own)
    assert CreateAccount(user=desired[0], uid=20005) in plan.actions


def test_rename_to_the_name_of_another_unix_account_changes_nothing():
    desired = [PaperlessUser(5, "mail", True)]
    actual = ActualState(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(workflow(7, "alice"),),
        unix_uids={"alice": 20005, "mail": 8},
    )
    plan = planned(desired, actual)
    assert plan.actions == ()


def test_rename_with_deactivation_disables_new_name():
    desired = [PaperlessUser(5, "dave", False)]
    plan = planned(desired, provisioned_state(username="alice", enabled=True))
    assert plan.actions == (
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
    # in a half-completed rename the old folder still carries the real
    # uid of a desired user; deleting it would destroy that user's data
    desired = [PaperlessUser(5, "dave", True)]
    actual = ActualState(
        accounts=(), folders=(InboxFolder("alice", 20005),), workflows=()
    )
    plan = planned(desired, actual)
    assert not any(isinstance(a, Deprovision) for a in plan.actions)
    assert any("alice" in w and "dave" in w for w in plan.warnings)


def test_stored_uid_below_the_range_does_not_count():
    # 19999 is the last uid outside the range, 20000 the first inside
    actual = ActualState(
        accounts=(SambaAccount("below", 19999, True), SambaAccount("edge", 20000, True)),
        folders=(),
        workflows=(),
    )
    plan = planned([], actual)
    assert plan.actions == (
        Deprovision(username="edge", workflow_id=None, delete_folder=False),
    )
    assert any("below" in w for w in plan.warnings)


def test_entry_without_stored_uid_is_created_again():
    desired = [PaperlessUser(5, "alice", True)]
    actual = ActualState(
        accounts=(SambaAccount("alice", None, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(workflow(7, "alice"),),
        unix_uids={"alice": 20005},
    )
    plan = planned(desired, actual)
    assert plan.actions == (CreateAccount(user=desired[0], uid=20005),)


def test_entry_without_stored_uid_and_without_user_is_left():
    actual = ActualState(
        accounts=(SambaAccount("ghost", None, True),), folders=(), workflows=()
    )
    plan = planned([], actual)
    assert plan.actions == ()
    assert any("ghost" in w for w in plan.warnings)


def test_missing_unix_account_is_repaired_from_the_stored_uid():
    desired = [PaperlessUser(5, "alice", True)]
    actual = ActualState(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(workflow(7, "alice"),),
    )
    plan = planned(desired, actual)
    assert plan.actions == (RepairUnixAccount(username="alice", uid=20005),)


def test_rename_with_reactivation_enables_new_name():
    desired = [PaperlessUser(5, "dave", True)]
    plan = planned(desired, provisioned_state(username="alice", enabled=False))
    assert plan.actions == (
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


def test_pending_document_goes_to_the_user_with_its_id():
    # uploaded for user 5 under the name alice, who is dave now, while user 9 took the name alice
    desired = [PaperlessUser(5, "dave", True), PaperlessUser(9, "alice", True)]
    actual = ActualState(
        accounts=(SambaAccount("alice", 20009, True), SambaAccount("dave", 20005, True)),
        folders=(InboxFolder("alice", 20009), InboxFolder("dave", 20005)),
        workflows=(workflow(8, "alice"), workflow(7, "dave")),
        unix_uids={"alice": 20009, "dave": 20005},
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 1, recipient_id=5),),
    )
    assert planned(desired, actual).actions == (
        HandOverDocument(document_id=40, user_id=5, username="dave"),
    )


def test_rename_retitles_the_document_of_the_user():
    desired = [PaperlessUser(5, "dave", True)]
    actual = replace(
        provisioned_state(),
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 5),),
    )
    assert (
        RenameDocument(document_id=40, old_username="alice", new_username="dave")
        in planned(desired, actual).actions
    )


def test_document_without_owner_or_account_is_deleted():
    actual = ActualState(
        accounts=(),
        folders=(),
        workflows=(),
        token_user=ADMIN,
        documents=(
            CredentialDocument(40, "alice", None),
            CredentialDocument(41, "bob", 1, recipient_id=6),
            CredentialDocument(42, "carol", 9),
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
            CredentialDocument(40, "alice", 5),
            CredentialDocument(41, "bob", 6),
            CredentialDocument(42, "bob", 1, recipient_id=6),
            CredentialDocument(43, "alice", None),
        ),
    )
    assert planned([], actual).actions == (
        Deprovision(
            username="alice", workflow_id=7, delete_folder=True, documents=(40, 43)
        ),
    )


def test_removal_of_the_token_user_leaves_the_uploads_for_other_users():
    # the user of the token is in the sync group and leaves it
    desired = [PaperlessUser(6, "bob", True)]
    actual = ActualState(
        accounts=(SambaAccount("admin", 20001, True), SambaAccount("bob", 20006, True)),
        folders=(InboxFolder("bob", 20006),),
        workflows=(workflow(8, "bob"),),
        unix_uids={"admin": 20001, "bob": 20006},
        token_user=ADMIN,
        documents=(
            CredentialDocument(40, "admin", 1),
            CredentialDocument(41, "bob", 1, recipient_id=6),
        ),
    )
    assert planned(desired, actual).actions == (
        Deprovision(
            username="admin", workflow_id=None, delete_folder=False, documents=(40,)
        ),
        HandOverDocument(document_id=41, user_id=6, username="bob"),
    )


def test_pending_document_of_another_owner_is_left_with_a_warning():
    desired = [PaperlessUser(5, "alice", True)]
    actual = replace(
        provisioned_state(),
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 9, recipient_id=5),),
    )
    plan = planned(desired, actual)
    assert plan.actions == ()
    assert any("40" in w for w in plan.warnings)
