import re

from .model import (
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
    folder_names = {f.name for f in actual.folders}
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
            if user.username not in folder_names:
                actions.append(CreateFolder(username=user.username, uid=uid))
            if user.username not in workflows_by_username:
                actions.append(CreateWorkflow(user_id=user.id, username=user.username))
            if not user.is_active:
                actions.append(DisableAccount(username=user.username))
            continue

        if account.uid != uid:
            warnings.append(
                f"account '{user.username}' has uid {account.uid}, expected {uid}; "
                f"left as-is"
            )
        if account.enabled and not user.is_active:
            actions.append(DisableAccount(username=user.username))
        elif not account.enabled and user.is_active:
            actions.append(EnableAccount(username=user.username))
        if user.username not in folder_names:
            actions.append(CreateFolder(username=user.username, uid=uid))
        if user.username not in workflows_by_username:
            actions.append(CreateWorkflow(user_id=user.id, username=user.username))

    protected_names = {a.username for a in actual.accounts if a.uid in invalid_uids}
    stale_accounts = {
        a.username
        for a in actual.accounts
        if a.uid >= uid_base
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
