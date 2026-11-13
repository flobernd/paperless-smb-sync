import re
from collections.abc import Container, Iterable, Mapping, Sequence

from .model import (
    Action,
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
    ManagedAccount,
    ManagedWorkflow,
    PaperlessUser,
    Plan,
    RenameDocument,
    RenameUser,
    RepairUnixAccount,
    SambaAccount,
    TokenUser,
    UpdateWorkflow,
)

NO_SUPERUSER_WARNING = (
    "CREDENTIALS_DOCUMENT is on, but the API token belongs to no superuser, so no "
    "credentials document is made"
)

# useradd-compatible names alone. A user with another name is skipped with a
# warning.
_VALID_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def counted_uid(account: SambaAccount, uid_base: int) -> int | None:
    # a creation stores the uid right after the password, so an entry without
    # one is either unfinished or not the daemon's
    if account.uid is not None and account.uid >= uid_base:
        return account.uid
    return None


def counted_accounts(
    accounts: Iterable[SambaAccount], uid_base: int
) -> tuple[ManagedAccount, ...]:
    return tuple(
        ManagedAccount(username=a.username, uid=uid, enabled=a.enabled)
        for a in accounts
        if (uid := counted_uid(a, uid_base)) is not None
    )


def _state_change(username: str, enabled: bool, active: bool) -> list[Action]:
    if enabled and not active:
        return [DisableAccount(username=username)]
    if not enabled and active:
        return [EnableAccount(username=username)]
    return []


def _skipped_state_change(
    user: PaperlessUser, uid: int, counted: Iterable[ManagedAccount]
) -> list[Action]:
    # A user that the planner skips can still hold a login under an earlier
    # name, and a deactivation has to close it. A rename that stopped leaves
    # the old and the new entry with the uid, and a restore gives the unix
    # account to the entry listed first, so each of them changes.
    return [
        action
        for holder in counted
        if holder.uid == uid
        for action in _state_change(holder.username, holder.enabled, user.is_active)
    ]


def _missing_folder(
    user: PaperlessUser, uid: int, folders_by_name: Mapping[str, InboxFolder]
) -> list[Action]:
    # an existing folder is adopted rather than recreated. Correcting its owner
    # uid also takes over a folder of that name that exists under another uid.
    folder = folders_by_name.get(user.username)
    if folder is None or folder.uid != uid:
        return [CreateFolder(username=user.username, uid=uid)]
    return []


def _missing_workflow(
    user: PaperlessUser,
    workflows_by_username: Mapping[str, ManagedWorkflow],
) -> list[Action]:
    workflow = workflows_by_username.get(user.username)
    if workflow is None:
        return [CreateWorkflow(user_id=user.id, username=user.username)]
    # a workflow that is the full definition for another user gives that user
    # the scans of this one
    if workflow.defined_for != user.id:
        return [
            UpdateWorkflow(
                workflow_id=workflow.id, user_id=user.id, username=user.username
            )
        ]
    return []


def user_documents(
    documents: Iterable[CredentialDocument], user_id: int, token_user: TokenUser
) -> tuple[CredentialDocument, ...]:
    # A title with an id marks an upload before its hand-over, which the
    # token's user owns and which can be meant for another user.
    return tuple(
        d
        for d in documents
        if (d.owner_id == user_id and d.recipient_id is None)
        or (d.owner_id == token_user.id and d.recipient_id == user_id)
    )


def _removal_documents(
    account: ManagedAccount,
    uid_base: int,
    desired_uids: Container[int],
    documents: Sequence[CredentialDocument],
    token_user: TokenUser | None,
) -> tuple[int, ...] | None:
    # the old name of a rename has the uid of a desired user, who keeps the
    # documents
    if token_user is None or account.uid in desired_uids:
        return None
    owned = {
        d.id for d in user_documents(documents, account.uid - uid_base, token_user)
    }
    return tuple(
        d.id
        for d in documents
        if d.id in owned or (d.owner_id is None and d.username == account.username)
    )


def _document_action(
    document: CredentialDocument,
    token_user: TokenUser,
    users_by_id: Mapping[int, PaperlessUser],
    desired_ids: Container[int],
    stored_uids: Container[int],
    uid_base: int,
) -> Action | None:
    if document.owner_id is None:
        # every user can read a document whose owner was deleted
        return DeleteDocument(document_id=document.id)
    if document.recipient_id is not None and document.owner_id == token_user.id:
        # the id decides, since another user can have taken the name that the
        # upload was made for
        user = users_by_id.get(document.recipient_id)
        if user is not None:
            # The title keeps the name that the text of the upload shows. When
            # the user has another name by now, the next cycle sees a title
            # that differs from the name of the owner and retitles the
            # document with a note.
            return HandOverDocument(
                document_id=document.id,
                user_id=user.id,
                username=document.username,
            )
        # while an entry stores the uid, the removal of that account takes the
        # document
        if (
            document.recipient_id not in desired_ids
            and uid_base + document.recipient_id not in stored_uids
        ):
            return DeleteDocument(document_id=document.id)
        return None
    if document.recipient_id is None:
        owner = users_by_id.get(document.owner_id)
        if owner is not None and owner.username != document.username:
            return RenameDocument(
                document_id=document.id,
                old_username=document.username,
                new_username=owner.username,
            )
    return None


def plan_cycle(
    users: list[PaperlessUser],
    actual: ActualState,
    uid_base: int,
) -> Plan:
    desired = [u for u in users if u.member]
    actions: list[Action] = []
    warnings: list[str] = []

    counted = counted_accounts(actual.accounts, uid_base)
    accounts_by_name = {a.username: a for a in counted}
    accounts_by_uid = {a.uid: a for a in counted}

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
                f"user '{user.username}' is not a valid unix account name and "
                f"was skipped. Rename the user in paperless to provision it."
            )
            actions.extend(_skipped_state_change(user, uid_base + user.id, counted))

    folders_by_name = {f.name: f for f in actual.folders}
    workflows_by_username = {w.name: w for w in actual.workflows}
    desired_names = {u.username for u in valid}

    delivery = actual.token_user
    if delivery is not None and not delivery.is_superuser:
        delivery = None

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
                actions.extend(_skipped_state_change(user, uid, counted))
                continue
            holder = accounts_by_uid.get(uid)
            if holder is not None and holder.username not in desired_names:
                # same uid under another name means the paperless user was
                # renamed, so the rename keeps the data and the password.
                # The state change of the old name does not depend on the
                # rename, so a deactivation closes the login when the rename
                # fails. The one of the new name sets the state of a copy that
                # an earlier rename made before it stopped and before the
                # state changed.
                actions.extend(
                    _state_change(holder.username, holder.enabled, user.is_active)
                )
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
                actions.extend(
                    _state_change(user.username, holder.enabled, user.is_active)
                )
                continue
            actions.append(
                CreateAccount(user=user, uid=uid, upload=delivery is not None)
            )
            actions.extend(_missing_folder(user, uid, folders_by_name))
            actions.extend(_missing_workflow(user, workflows_by_username))
            # smbpasswd -a creates the account enabled
            actions.extend(_state_change(user.username, True, user.is_active))
            continue

        if user.username not in actual.unix_uids:
            # passdb survived a recreation of the container but the unix
            # account of this entry did not. smbd needs that account to map the
            # NT hash.
            # create_unix_user is idempotent, so this is safe to re-emit.
            actions.append(RepairUnixAccount(username=user.username, uid=account.uid))
        if account.uid != uid:
            # The entry can be the account of a former holder of the name. A
            # chown would give the folder of that holder to the new user, an
            # enable would open the old login again, and a workflow for the new
            # user would give that user the scans of the old login. Closing the
            # login gives nothing to anyone, and after a lowered UID_BASE the
            # login is the member's own, so a deactivation still closes it.
            warnings.append(
                f"account '{user.username}' stores uid {account.uid}, but the "
                f"paperless user of that name maps to uid {uid}. The daemon "
                f"leaves the folder and the workflow of this name as they are "
                f"and does not enable the login. If another user had the name "
                f"before, take the user out of the sync group until the old "
                f"account is gone, then add the user again."
            )
            if account.enabled and not user.is_active:
                actions.append(DisableAccount(username=user.username))
            continue
        actions.extend(_state_change(user.username, account.enabled, user.is_active))
        actions.extend(_missing_folder(user, uid, folders_by_name))
        actions.extend(_missing_workflow(user, workflows_by_username))

    claimed_names = desired_names | renamed_from
    protected_names = {a.username for a in counted if a.uid in invalid_uids}

    for account in actual.accounts:
        if (
            counted_uid(account, uid_base) is None
            and account.username not in desired_names
        ):
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
                f"interrupted rename) and was left untouched"
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
    if actual.token_user is not None and delivery is None:
        warnings.append(NO_SUPERUSER_WARNING)
    deprovisioned = set(candidates[:1])
    held: set[int] = set()
    for name in deprovisioned:
        workflow = workflows_by_username.get(name)
        inbox = folders_by_name.get(name)
        documents = _removal_documents(
            stale_accounts[name],
            uid_base,
            expected_uid_owner,
            actual.documents,
            delivery,
        )
        held.update(documents or ())
        actions.append(
            Deprovision(
                username=name,
                user_id=stale_accounts[name].uid - uid_base,
                workflow_id=workflow.id if workflow else None,
                delete_folder=inbox is not None
                and inbox.uid == stale_accounts[name].uid
                and inbox.uid not in expected_uid_owner,
                documents=documents,
            )
        )

    # paperless imports a scan with or without a workflow, and the workflow
    # only sets the owner. So the workflow of an account that waits for its
    # removal goes inside that removal, after the login closed, and a scan
    # that arrives while the login of a user who left the group is still open
    # still gets that user as its owner.
    for workflow in actual.workflows:
        if (
            workflow.name not in claimed_names
            and workflow.name not in stale_accounts
            and workflow.name not in protected_names
        ):
            actions.append(DeleteWorkflow(workflow_id=workflow.id))

    if delivery is not None:
        users_by_id = {u.id: u for u in valid}
        desired_ids = {u.id for u in desired}
        for document in actual.documents:
            if document.id in held:
                continue
            if document.recipient_id is not None and document.owner_id not in (
                None,
                delivery.id,
            ):
                warnings.append(
                    f"credentials document {document.id} waits for user "
                    f"{document.recipient_id} but the user of the token does not own "
                    f"it, so it was left untouched. A paperless workflow that sets "
                    f"the owner of an upload can cause this."
                )
                continue
            action = _document_action(
                document, delivery, users_by_id, desired_ids, accounts_by_uid, uid_base
            )
            if action is not None:
                actions.append(action)

    return Plan(
        actions=tuple(actions),
        warnings=tuple(warnings),
        pending_removal=tuple(candidates),
    )
