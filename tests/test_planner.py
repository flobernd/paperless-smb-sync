from smb_sync.model import (
    ActualState,
    CreateAccount,
    CreateFolder,
    CreateWorkflow,
    DeleteAccount,
    DeleteFolder,
    DeleteWorkflow,
    DisableAccount,
    EnableAccount,
    InboxFolder,
    ManagedWorkflow,
    PaperlessUser,
    RenameUser,
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
    )


def test_provision_new_user():
    desired = [PaperlessUser(5, "alice", True)]
    plan = plan_cycle(desired, empty_state(), UID_BASE)
    assert plan.actions == (
        CreateAccount(user=desired[0], uid=20005),
        CreateFolder(username="alice", uid=20005),
        CreateWorkflow(user_id=5, username="alice"),
    )
    assert plan.warnings == ()


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
    )
    plan = plan_cycle(desired, actual, UID_BASE)
    assert plan.actions == (
        CreateFolder(username="alice", uid=20005),
        CreateWorkflow(user_id=5, username="alice"),
    )


def test_deprovision_removes_workflow_account_folder_in_order():
    plan = plan_cycle([], provisioned_state(), UID_BASE)
    assert plan.actions == (
        DeleteWorkflow(workflow_id=7),
        DeleteAccount(username="alice"),
        DeleteFolder(username="alice"),
    )


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
    deleted = [a for a in plan.actions if isinstance(a, DeleteAccount)]
    assert deleted == [DeleteAccount(username="alice")]
    assert any("bob" in w and "deferred" in w for w in plan.warnings)
    # bob's workflow removal is non-destructive and not deferred
    assert DeleteWorkflow(workflow_id=8) in plan.actions
    assert DeleteFolder(username="bob") not in plan.actions


def test_unmanaged_account_untouched():
    actual = ActualState(
        accounts=(SambaAccount("legacy", 1000, True),),
        folders=(),
        workflows=(),
    )
    plan = plan_cycle([], actual, UID_BASE)
    assert plan.actions == ()


def test_invalid_username_skipped_with_warning():
    desired = [PaperlessUser(5, "alice@example.com", True)]
    plan = plan_cycle(desired, empty_state(), UID_BASE)
    assert plan.actions == ()
    assert any("alice@example.com" in w for w in plan.warnings)


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


def test_orphan_managed_workflow_deleted():
    actual = ActualState(
        accounts=(), folders=(), workflows=(ManagedWorkflow(7, "ghost"),)
    )
    plan = plan_cycle([], actual, UID_BASE)
    assert plan.actions == (DeleteWorkflow(workflow_id=7),)


def test_orphan_managed_folder_deleted():
    actual = ActualState(
        accounts=(), folders=(InboxFolder("ghost", 20009),), workflows=()
    )
    plan = plan_cycle([], actual, UID_BASE)
    assert plan.actions == (DeleteFolder(username="ghost"),)


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
    assert not any(isinstance(a, (DeleteAccount, RenameUser)) for a in plan.actions)
    assert any("uid" in w.lower() for w in plan.warnings)
