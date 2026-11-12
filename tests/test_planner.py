from smb_sync.model import (
    ActualState,
    CreateAccount,
    CreateFolder,
    CreateWorkflow,
    DeleteWorkflow,
    Deprovision,
    DisableAccount,
    EnableAccount,
    InboxFolder,
    ManagedWorkflow,
    PaperlessUser,
    RenameUser,
    RepairUnixAccount,
    SambaAccount,
)
from smb_sync.planner import plan_cycle

UID_BASE = 20000


def empty_state():
    return ActualState(accounts=(), folders=(), workflows=())


def provisioned_state(username="alice", user_id=5, enabled=True, workflow_id=7):
    return ActualState(
        accounts=(SambaAccount(username, UID_BASE + user_id, enabled),),
        folders=(InboxFolder(username, UID_BASE + user_id),),
        workflows=(ManagedWorkflow(workflow_id, username),),
        unix_uids={username: UID_BASE + user_id},
    )


def test_steady_state_no_actions():
    desired = [PaperlessUser(5, "alice", True)]
    plan = plan_cycle(desired, provisioned_state(), UID_BASE)
    assert plan.actions == ()


def test_inactive_user_disabled():
    desired = [PaperlessUser(5, "alice", False)]
    plan = plan_cycle(desired, provisioned_state(enabled=True), UID_BASE)
    assert plan.actions == (DisableAccount(username="alice"),)


def test_reactivated_user_enabled():
    desired = [PaperlessUser(5, "alice", True)]
    plan = plan_cycle(desired, provisioned_state(enabled=False), UID_BASE)
    assert plan.actions == (EnableAccount(username="alice"),)


def test_new_inactive_user_provisioned_disabled():
    desired = [PaperlessUser(5, "alice", False)]
    plan = plan_cycle(desired, empty_state(), UID_BASE)
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
    plan = plan_cycle(desired, actual, UID_BASE)
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
        workflows=(ManagedWorkflow(7, "alice"),),
        unix_uids={"alice": 20005},
    )
    plan = plan_cycle(desired, actual, UID_BASE)
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
    plan = plan_cycle(desired, actual, UID_BASE)
    assert plan.actions == (
        CreateAccount(user=desired[0], uid=20005),
        CreateFolder(username="alice", uid=20005),
        CreateWorkflow(user_id=5, username="alice"),
    )


def test_removal_takes_the_workflow_and_the_folder_of_the_account():
    plan = plan_cycle([], provisioned_state(), UID_BASE)
    assert plan.actions == (
        Deprovision(username="alice", workflow_id=7, delete_folder=True),
    )


def test_stale_account_keeps_a_folder_of_another_uid():
    actual = ActualState(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20009),),
        workflows=(),
    )
    plan = plan_cycle([], actual, UID_BASE)
    assert plan.actions == (
        Deprovision(username="alice", workflow_id=None, delete_folder=False),
    )


def test_stale_folder_beside_an_entry_that_does_not_count_deletes_nothing():
    actual = ActualState(
        accounts=(SambaAccount("bob", None, True),),
        folders=(InboxFolder("bob", 20006),),
        workflows=(),
    )
    plan = plan_cycle([], actual, UID_BASE)
    assert plan.actions == ()


def test_at_most_one_deprovision_per_cycle():
    actual = ActualState(
        accounts=(
            SambaAccount("alice", 20005, True),
            SambaAccount("bob", 20006, True),
        ),
        folders=(InboxFolder("alice", 20005), InboxFolder("bob", 20006)),
        workflows=(ManagedWorkflow(7, "alice"), ManagedWorkflow(8, "bob")),
    )
    plan = plan_cycle([], actual, UID_BASE)
    removals = [a for a in plan.actions if isinstance(a, Deprovision)]
    assert removals == [
        Deprovision(username="alice", workflow_id=7, delete_folder=True)
    ]
    assert any("bob" in w and "deferred" in w for w in plan.warnings)
    # bob's workflow removal is non-destructive and not deferred
    assert DeleteWorkflow(workflow_id=8) in plan.actions


def test_unmanaged_account_untouched():
    actual = ActualState(
        accounts=(SambaAccount("legacy", 1000, True),),
        folders=(),
        workflows=(),
    )
    plan = plan_cycle([], actual, UID_BASE)
    assert plan.actions == ()


def test_rename_detected_by_uid():
    desired = [PaperlessUser(5, "dave", True)]
    plan = plan_cycle(desired, provisioned_state(username="alice"), UID_BASE)
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
    plan = plan_cycle([], actual, UID_BASE)
    assert plan.actions == ()
    assert any("archive" in w for w in plan.warnings)


def test_unmanaged_folder_warned_not_deleted():
    actual = ActualState(
        accounts=(), folders=(InboxFolder("manual", 0),), workflows=()
    )
    plan = plan_cycle([], actual, UID_BASE)
    assert plan.actions == ()
    assert any("manual" in w for w in plan.warnings)


def test_uid_mismatch_warned_not_touched():
    desired = [PaperlessUser(6, "alice", True)]
    plan = plan_cycle(desired, provisioned_state(username="alice", user_id=5), UID_BASE)
    assert not any(isinstance(a, (Deprovision, RenameUser)) for a in plan.actions)
    assert any("uid" in w.lower() for w in plan.warnings)


def test_invalid_rename_preserves_existing_resources():
    desired = [PaperlessUser(5, "alice@example.com", True)]
    plan = plan_cycle(desired, provisioned_state(), UID_BASE)
    assert plan.actions == ()
    assert any("alice@example.com" in w for w in plan.warnings)
    assert not any("folder" in w for w in plan.warnings)


def test_rename_with_deactivation_disables_new_name():
    desired = [PaperlessUser(5, "dave", False)]
    plan = plan_cycle(
        desired, provisioned_state(username="alice", enabled=True), UID_BASE
    )
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
    plan = plan_cycle(desired, actual, UID_BASE)
    assert not any(isinstance(a, Deprovision) for a in plan.actions)
    assert any("alice" in w and "dave" in w for w in plan.warnings)


def test_stored_uid_below_the_range_does_not_count():
    # 19999 is the last uid outside the range, 20000 the first inside
    actual = ActualState(
        accounts=(SambaAccount("below", 19999, True), SambaAccount("edge", 20000, True)),
        folders=(),
        workflows=(),
    )
    plan = plan_cycle([], actual, UID_BASE)
    assert plan.actions == (
        Deprovision(username="edge", workflow_id=None, delete_folder=False),
    )
    assert any("below" in w for w in plan.warnings)


def test_entry_without_stored_uid_is_created_again():
    desired = [PaperlessUser(5, "alice", True)]
    actual = ActualState(
        accounts=(SambaAccount("alice", None, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(ManagedWorkflow(7, "alice"),),
        unix_uids={"alice": 20005},
    )
    plan = plan_cycle(desired, actual, UID_BASE)
    assert plan.actions == (CreateAccount(user=desired[0], uid=20005),)


def test_entry_without_stored_uid_and_without_user_is_left():
    actual = ActualState(
        accounts=(SambaAccount("ghost", None, True),), folders=(), workflows=()
    )
    plan = plan_cycle([], actual, UID_BASE)
    assert plan.actions == ()
    assert any("ghost" in w for w in plan.warnings)


def test_missing_unix_account_is_repaired_from_the_stored_uid():
    desired = [PaperlessUser(5, "alice", True)]
    actual = ActualState(
        accounts=(SambaAccount("alice", 20005, True),),
        folders=(InboxFolder("alice", 20005),),
        workflows=(ManagedWorkflow(7, "alice"),),
    )
    plan = plan_cycle(desired, actual, UID_BASE)
    assert plan.actions == (RepairUnixAccount(username="alice", uid=20005),)


def test_rename_with_reactivation_enables_new_name():
    desired = [PaperlessUser(5, "dave", True)]
    plan = plan_cycle(
        desired, provisioned_state(username="alice", enabled=False), UID_BASE
    )
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
