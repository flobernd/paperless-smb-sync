import re
from collections.abc import Iterable, Mapping

from .model import (
    Action,
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
    Plan,
    RenameUser,
    RepairUnixAccount,
    SambaAccount,
    UpdateWorkflow,
    workflow_filter,
)

# useradd-compatible names only; anything else is skipped with a warning
_VALID_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def counted_accounts(
    accounts: Iterable[SambaAccount], uid_base: int
) -> tuple[SambaAccount, ...]:
    # the uid goes into an entry last in a creation, so an entry without one
    # is either unfinished or not the daemon's
    return tuple(a for a in accounts if a.uid is not None and a.uid >= uid_base)


def _state_change(username: str, enabled: bool, active: bool) -> list[Action]:
    if enabled and not active:
        return [DisableAccount(username=username)]
    if not enabled and active:
        return [EnableAccount(username=username)]
    return []


def _missing_resources(
    user: PaperlessUser,
    uid: int,
    folders_by_name: Mapping[str, InboxFolder],
    workflows_by_username: Mapping[str, ManagedWorkflow],
    consume_path: str,
) -> list[Action]:
    actions: list[Action] = []
    # an existing folder is adopted rather than recreated; correcting its
    # owner uid also brings a pre-migration folder under management
    folder = folders_by_name.get(user.username)
    if folder is None or folder.uid != uid:
        actions.append(CreateFolder(username=user.username, uid=uid))
    workflow = workflows_by_username.get(user.username)
    if workflow is None:
        actions.append(CreateWorkflow(user_id=user.id, username=user.username))
    elif workflow.filter_path != workflow_filter(consume_path, user.username):
        actions.append(
            UpdateWorkflow(
                workflow_id=workflow.id, user_id=user.id, username=user.username
            )
        )
    return actions


def plan_cycle(
    desired: list[PaperlessUser],
    actual: ActualState,
    uid_base: int,
    consume_path: str,
) -> Plan:
    actions: list[Action] = []
    warnings: list[str] = []

    valid: list[PaperlessUser] = []
    # uids of skipped users still protect their resources: a rename to an
    # invalid name must not tear down what the old name provisioned
    invalid_uids: set[int] = set()
    for user in desired:
        if _VALID_USERNAME.match(user.username):
            valid.append(user)
        else:
            invalid_uids.add(uid_base + user.id)
            warnings.append(
                f"user '{user.username}' is not a valid unix account name; "
                f"skipped (rename the user in paperless to provision it)"
            )

    counted = counted_accounts(actual.accounts, uid_base)
    accounts_by_name = {a.username: a for a in counted}
    accounts_by_uid = {a.uid: a for a in counted}
    folders_by_name = {f.name: f for f in actual.folders}
    workflows_by_username = {w.username: w for w in actual.workflows}
    desired_names = {u.username for u in valid}

    renamed_from: set[str] = set()

    for user in valid:
        uid = uid_base + user.id
        account = accounts_by_name.get(user.username)

        if account is None:
            # a unix account with the user's own uid is what a creation that
            # stopped early leaves behind
            taken_uid = actual.unix_uids.get(user.username)
            if taken_uid is not None and taken_uid != uid:
                invalid_uids.add(uid)
                warnings.append(
                    f"user '{user.username}' has the name of another unix account "
                    f"(uid {taken_uid}) and was skipped. Rename the user in "
                    f"paperless to provision it."
                )
                continue
            holder = accounts_by_uid.get(uid)
            if holder is not None and holder.username not in desired_names:
                # same uid under another name means the paperless user was
                # renamed; converge without touching data or the password
                workflow = workflows_by_username.get(holder.username)
                actions.append(
                    RenameUser(
                        old_username=holder.username,
                        new_username=user.username,
                        uid=uid,
                        user_id=user.id,
                        workflow_id=workflow.id if workflow else None,
                    )
                )
                renamed_from.add(holder.username)
                # the executor renames before the state change, so the
                # enable/disable action targets the new name
                actions.extend(
                    _state_change(user.username, holder.enabled, user.is_active)
                )
                continue
            actions.append(CreateAccount(user=user, uid=uid))
            actions.extend(
                _missing_resources(
                    user, uid, folders_by_name, workflows_by_username, consume_path
                )
            )
            # smbpasswd -a creates the account enabled
            actions.extend(_state_change(user.username, True, user.is_active))
            continue

        if user.username not in actual.unix_uids:
            # passdb survived but its unix account did not (container
            # recreation); rebuild it so smbd can map the NT hash again.
            # create_unix_user is idempotent, so this is safe to re-emit.
            actions.append(
                RepairUnixAccount(username=user.username, uid=account.uid)
            )
        missing = _missing_resources(
            user, uid, folders_by_name, workflows_by_username, consume_path
        )
        if account.uid != uid:
            warnings.append(
                f"account '{user.username}' has uid {account.uid}, expected {uid}; "
                f"left as-is"
            )
            # the planner cannot tell whose data the folder holds, so a chown
            # would hand it to the paperless user who has the name now
            missing = [a for a in missing if not isinstance(a, CreateFolder)]
        actions.extend(_state_change(user.username, account.enabled, user.is_active))
        actions.extend(missing)

    claimed_names = desired_names | renamed_from
    protected_names = {a.username for a in counted if a.uid in invalid_uids}

    for account in actual.accounts:
        if account not in counted and account.username not in desired_names:
            warnings.append(
                f"samba entry '{account.username}' stores no uid of the managed "
                f"range and was left untouched"
            )

    # a folder carrying the uid a desired user maps to still holds that
    # user's data even under a stale name (half-completed rename), so it
    # must never be deleted
    expected_uid_owner = {uid_base + u.id: u.username for u in valid}

    stale_accounts = {
        a.username: a
        for a in counted
        if a.uid not in invalid_uids and a.username not in claimed_names
    }

    for folder in actual.folders:
        if folder.name in claimed_names:
            continue
        if folder.uid < uid_base:
            warnings.append(
                f"unmanaged folder '{folder.name}' (uid {folder.uid}) left untouched"
            )
        elif folder.uid in expected_uid_owner:
            warnings.append(
                f"folder '{folder.name}' has uid {folder.uid} belonging to "
                f"user '{expected_uid_owner[folder.uid]}' (possibly an "
                f"interrupted rename); left untouched"
            )
        elif (
            folder.name not in accounts_by_name
            or accounts_by_name[folder.name].uid != folder.uid
        ):
            # a folder alone does not prove that the daemon made it, so it
            # goes only together with its account
            warnings.append(
                f"folder '{folder.name}' (uid {folder.uid}) belongs to no managed "
                f"samba account and was left untouched"
            )

    # a removal closes the login as its first step, so an enabled entry is one
    # that no removal has tried yet. Taking those first keeps a removal that
    # fails in each cycle from holding the login of another former user open.
    candidates = sorted(
        stale_accounts, key=lambda name: (not stale_accounts[name].enabled, name)
    )
    for name in candidates[1:]:
        warnings.append(
            f"deprovisioning of '{name}' deferred to a later cycle (one per cycle)"
        )
    deprovisioned = set(candidates[:1])
    for name in deprovisioned:
        workflow = workflows_by_username.get(name)
        inbox = folders_by_name.get(name)
        actions.append(
            Deprovision(
                username=name,
                workflow_id=workflow.id if workflow else None,
                delete_folder=inbox is not None
                and inbox.uid == stale_accounts[name].uid
                and inbox.uid not in expected_uid_owner,
            )
        )

    # stray managed workflows only stop intake, so removing them all at once
    # is safe even when the matching account removal is deferred
    for workflow in actual.workflows:
        if (
            workflow.username not in claimed_names
            and workflow.username not in deprovisioned
            and workflow.username not in protected_names
        ):
            actions.append(DeleteWorkflow(workflow_id=workflow.id))

    return Plan(
        actions=tuple(actions),
        warnings=tuple(warnings),
        pending_removal=tuple(candidates),
    )
