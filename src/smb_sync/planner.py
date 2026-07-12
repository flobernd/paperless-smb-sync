import re

from .model import (
    UNMAPPED_UID,
    Action,
    ActualState,
    CreateAccount,
    CreateFolder,
    CreateWorkflow,
    DeleteAccount,
    DeleteFolder,
    DeleteWorkflow,
    DisableAccount,
    EnableAccount,
    PaperlessUser,
    Plan,
    RenameUser,
    RepairUnixAccount,
)

# useradd-compatible names only; anything else is skipped with a warning
_VALID_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def plan_cycle(
    desired: list[PaperlessUser], actual: ActualState, uid_base: int
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

    accounts_by_name = {a.username: a for a in actual.accounts}
    accounts_by_uid = {a.uid: a for a in actual.accounts}
    folders_by_name = {f.name: f for f in actual.folders}
    workflows_by_username = {w.username: w for w in actual.workflows}
    desired_names = {u.username for u in valid}

    renamed_from: set[str] = set()

    for user in valid:
        uid = uid_base + user.id
        account = accounts_by_name.get(user.username)

        if account is None:
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
                if holder.enabled and not user.is_active:
                    actions.append(DisableAccount(username=user.username))
                elif not holder.enabled and user.is_active:
                    actions.append(EnableAccount(username=user.username))
                continue
            actions.append(CreateAccount(user=user, uid=uid))
            # an existing folder is adopted rather than recreated; correcting
            # its owner uid also brings a pre-migration folder under management
            folder = folders_by_name.get(user.username)
            if folder is None or folder.uid != uid:
                actions.append(CreateFolder(username=user.username, uid=uid))
            if user.username not in workflows_by_username:
                actions.append(CreateWorkflow(user_id=user.id, username=user.username))
            if not user.is_active:
                actions.append(DisableAccount(username=user.username))
            continue

        if account.uid == UNMAPPED_UID:
            # passdb survived but its unix account did not (container
            # recreation); rebuild it so smbd can map the NT hash again.
            # create_unix_user is idempotent, so this is safe to re-emit.
            actions.append(RepairUnixAccount(username=user.username, uid=uid))
        elif account.uid != uid:
            warnings.append(
                f"account '{user.username}' has uid {account.uid}, expected {uid}; "
                f"left as-is"
            )
        if account.enabled and not user.is_active:
            actions.append(DisableAccount(username=user.username))
        elif not account.enabled and user.is_active:
            actions.append(EnableAccount(username=user.username))
        folder = folders_by_name.get(user.username)
        if folder is None or folder.uid != uid:
            actions.append(CreateFolder(username=user.username, uid=uid))
        if user.username not in workflows_by_username:
            actions.append(CreateWorkflow(user_id=user.id, username=user.username))

    protected_names = {a.username for a in actual.accounts if a.uid in invalid_uids}

    # an unmapped uid means the unix account is gone, likely a rename that
    # crashed halfway; the passdb entry holds the only copy of the NT hash,
    # so deprovisioning it would make the rename unrecoverable
    for account in actual.accounts:
        if (
            account.uid == UNMAPPED_UID
            and account.username not in desired_names
            and account.username not in renamed_from
        ):
            warnings.append(
                f"samba account '{account.username}' has no unix account "
                f"(possibly an interrupted rename); left untouched"
            )

    # a folder carrying the uid a desired user maps to still holds that
    # user's data even under a stale name (half-completed rename), so it
    # must never be deleted
    expected_uid_owner = {uid_base + u.id: u.username for u in valid}
    held_folder_names: set[str] = set()
    for folder in actual.folders:
        if (
            folder.uid in expected_uid_owner
            and folder.name not in desired_names
            and folder.name not in renamed_from
        ):
            held_folder_names.add(folder.name)
            warnings.append(
                f"folder '{folder.name}' has uid {folder.uid} belonging to "
                f"user '{expected_uid_owner[folder.uid]}' (possibly an "
                f"interrupted rename); left untouched"
            )

    stale_accounts = {
        a.username
        for a in actual.accounts
        if a.uid >= uid_base
        and a.uid != UNMAPPED_UID
        and a.uid not in invalid_uids
        and a.username not in desired_names
        and a.username not in renamed_from
    }
    stale_folders = {
        f.name
        for f in actual.folders
        if f.uid >= uid_base
        and f.uid not in invalid_uids
        and f.name not in desired_names
        and f.name not in renamed_from
        and f.name not in held_folder_names
    }
    for folder in actual.folders:
        if (
            folder.uid < uid_base
            and folder.name not in desired_names
            and folder.name not in renamed_from
        ):
            warnings.append(
                f"unmanaged folder '{folder.name}' (uid {folder.uid}) left untouched"
            )

    candidates = sorted(stale_accounts | stale_folders)
    for name in candidates[1:]:
        warnings.append(
            f"deprovisioning of '{name}' deferred to a later cycle (one per cycle)"
        )
    deprovisioned: set[str] = set(candidates[:1])
    for name in candidates[:1]:
        workflow = workflows_by_username.get(name)
        if workflow is not None:
            actions.append(DeleteWorkflow(workflow_id=workflow.id))
        if name in accounts_by_name:
            actions.append(DeleteAccount(username=name))
        if name in stale_folders:
            actions.append(DeleteFolder(username=name))

    # stray managed workflows only stop intake, so removing them all at once
    # is safe even when the matching account removal is deferred
    for workflow in actual.workflows:
        if (
            workflow.username not in desired_names
            and workflow.username not in renamed_from
            and workflow.username not in deprovisioned
            and workflow.username not in protected_names
        ):
            actions.append(DeleteWorkflow(workflow_id=workflow.id))

    return Plan(actions=tuple(actions), warnings=tuple(warnings))
