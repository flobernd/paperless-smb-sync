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
    RepairFolder,
    RepairUnixAccount,
    SambaAccount,
    TokenUser,
    UpdateWorkflow,
    WriteMap,
    account_name,
    login_key,
    map_logins,
    parse_mark,
    render_map,
)

NO_SUPERUSER_WARNING = (
    "CREDENTIALS_DOCUMENT is on, but the API token belongs to no superuser, so no "
    "credentials document is made"
)

# Samba reads a name of the map that starts with @, + or & as a group or a
# netgroup. The pattern is the username set of the paperless validator.
_MAP_SIGNS = ("@", "+", "&")
_PAPERLESS_USERNAME = re.compile(r"[\w.@+-]+")


def counted_accounts(
    accounts: Iterable[SambaAccount], uid_base: int
) -> tuple[ManagedAccount, ...]:
    # A creation stores the mark as its last step on the entry, so an entry
    # without one is either unfinished or not the daemon's. The daemon names each
    # account after its uid, so an entry that carries a mark under another name
    # is not one that it made.
    counted: list[ManagedAccount] = []
    for account in accounts:
        mark = parse_mark(account.description)
        if mark is None:
            continue
        uid, joined = mark
        if uid >= uid_base and account.username == account_name(uid):
            counted.append(
                ManagedAccount(
                    name=account.username,
                    uid=uid,
                    user_id=uid - uid_base,
                    joined=joined,
                    enabled=account.enabled,
                )
            )
    return tuple(sorted(counted, key=lambda a: a.uid))


def members_by_uid(
    users: Iterable[PaperlessUser], uid_base: int
) -> dict[int, PaperlessUser]:
    return {uid_base + u.id: u for u in users if u.member}


def _login_names(
    members: Mapping[int, PaperlessUser], read_logins: Mapping[str, str]
) -> tuple[dict[str, str], list[str]]:
    warnings: list[str] = []
    contenders: dict[tuple[str, ...], list[int]] = {}
    for uid in sorted(members):
        username = members[uid].username
        if username.startswith(_MAP_SIGNS):
            warnings.append(
                f"user '{username}' gets no login name: a login name cannot start "
                f"with '@', '+' or '&'. Rename the user in paperless."
            )
        elif _PAPERLESS_USERNAME.fullmatch(username) is None:
            warnings.append(
                f"user '{username}' gets no login name: a login name holds only "
                f"letters, digits, '_', '.', '@', '+' and '-'. Rename the user in "
                f"paperless."
            )
        else:
            contenders.setdefault(login_key(username), []).append(uid)

    kept: list[int] = []
    for key, uids in contenders.items():
        # The member who has the name in the map now keeps it, so that a user
        # who takes the name in another case cannot take over the login of
        # another person.
        holders = [
            uid
            for uid in uids
            if (login := read_logins.get(account_name(uid))) is not None
            and login_key(login) == key
        ]
        keeper_uid = holders[0] if len(holders) == 1 else uids[0]
        kept.append(keeper_uid)
        keeper = members[keeper_uid]
        for uid in uids:
            if uid != keeper_uid:
                user = members[uid]
                warnings.append(
                    f"user '{user.username}' (id {user.id}) gets no login name: "
                    f"user '{keeper.username}' (id {keeper.id}) keeps this name, "
                    f"and Samba compares login names without case. Rename one of "
                    f"the two users in paperless."
                )
    logins = {account_name(uid): members[uid].username for uid in sorted(kept)}
    return logins, warnings


def _workflow_action(
    user: PaperlessUser, account: str, workflows: Mapping[str, ManagedWorkflow]
) -> list[Action]:
    workflow = workflows.get(account)
    if workflow is None:
        return [CreateWorkflow(user_id=user.id, account=account)]
    # a workflow that is the full definition for another user gives that user
    # the scans of this one
    if workflow.defined_for != user.id:
        return [
            UpdateWorkflow(workflow_id=workflow.id, user_id=user.id, account=account)
        ]
    return []


def _member_actions(
    user: PaperlessUser,
    account: ManagedAccount,
    unix_names: Container[str],
    session_accounts: Container[str],
    folders: Mapping[str, InboxFolder],
    workflows: Mapping[str, ManagedWorkflow],
    paperless_gid: int,
) -> list[Action]:
    actions: list[Action] = []
    if account.name not in unix_names:
        # passdb survived a recreation of the container but the unix
        # account of this entry did not. smbd needs that account to map the
        # NT hash.
        # create_unix_user is idempotent, so this is safe to re-emit.
        actions.append(RepairUnixAccount(account=account.name, uid=account.uid))
    # paperless imports a scan with or without a workflow, and only a workflow
    # that is the full definition for this member gives the scan its owner.
    # The login closes before the workflow is written back and opens in a
    # later cycle that finds the workflow, so a failed write leaves it closed.
    workflow = workflows.get(account.name)
    may_open = (
        user.is_active and workflow is not None and workflow.defined_for == user.id
    )
    if (account.enabled or account.name in session_accounts) and not may_open:
        actions.append(DisableAccount(account=account.name))
    # an existing folder is adopted rather than recreated, and the repair of its
    # owner also takes over a folder of that name under another uid. paperless
    # enters a folder of mode 2770 through its group alone, so a folder that
    # kept the group from before a change of PAPERLESS_GID is repaired too.
    folder = folders.get(account.name)
    if folder is None:
        actions.append(CreateFolder(account=account.name, uid=account.uid))
    elif folder.uid != account.uid or folder.gid != paperless_gid:
        actions.append(RepairFolder(account=account.name, uid=account.uid))
    actions.extend(_workflow_action(user, account.name, workflows))
    if not account.enabled and may_open:
        actions.append(EnableAccount(account=account.name))
    return actions


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
    user_id: int,
    login_name: str | None,
    documents: Sequence[CredentialDocument],
    token_user: TokenUser | None,
) -> tuple[int, ...] | None:
    if token_user is None:
        return None
    owned = {d.id for d in user_documents(documents, user_id, token_user)}
    # paperless clears the owner of a document when it deletes the user, and
    # the login name in the title is then all that links the document to the
    # account
    return tuple(
        d.id
        for d in documents
        if d.id in owned or (d.owner_id is None and d.username == login_name)
    )


def _document_action(
    document: CredentialDocument,
    token_user: TokenUser,
    users_with_line: Mapping[int, PaperlessUser],
    member_ids: Container[int],
) -> Action | None:
    if document.owner_id is None:
        # every user can read a document whose owner was deleted
        return DeleteDocument(document_id=document.id)
    if document.recipient_id is not None and document.owner_id == token_user.id:
        # the id decides, since another user can have taken the name that the
        # upload was made for
        user = users_with_line.get(document.recipient_id)
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
        if document.recipient_id not in member_ids:
            return DeleteDocument(document_id=document.id)
        return None
    if document.recipient_id is None:
        owner = users_with_line.get(document.owner_id)
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
    paperless_gid: int,
) -> Plan:
    actions: list[Action] = []
    warnings: list[str] = []

    users_by_uid = {uid_base + u.id: u for u in users}
    members = members_by_uid(users, uid_base)
    counted = counted_accounts(actual.accounts, uid_base)
    counted_names = {a.name for a in counted}
    entry_uids = {a.uid for a in counted}
    known_uids: set[int] = set()
    foreign_uids: set[int] = set()
    for account in counted:
        user = users_by_uid.get(account.uid)
        if user is None:
            continue
        if user.joined == account.joined:
            known_uids.add(account.uid)
        else:
            foreign_uids.add(account.uid)
    folders = {f.name: f for f in actual.folders}
    workflows = {w.name: w for w in actual.workflows}
    read_logins = map_logins(actual.map_lines)

    # A hold closes logins and never deletes, because a closed login opens
    # again when the cause ends and a deleted folder cannot come back.
    hold_cause: str | None = None
    if foreign_uids:
        hold_cause = "the Samba volume holds an entry of another paperless user"
    elif counted and not known_uids:
        hold_cause = "paperless knows none of the accounts on the Samba volume"
    hold = hold_cause is not None

    delivery = actual.token_user
    if delivery is not None and not delivery.is_superuser:
        delivery = None

    # The daemon makes the folder after the entry, so a folder under the name
    # of an account without an entry is not one that it made for this member,
    # and it can hold the files of an earlier user of the uid.
    folder_stops = {
        account_name(uid): user
        for uid, user in members.items()
        if uid not in entry_uids and account_name(uid) in folders
    }
    candidates = {
        uid: user
        for uid, user in members.items()
        if uid in known_uids
        or (
            uid not in entry_uids
            and not hold
            and account_name(uid) not in folder_stops
        )
    }

    # The lines are in the order of the uids, which does not change with a
    # rename or with the order in which paperless lists the users, so a map
    # that is up to date compares equal.
    logins, name_warnings = _login_names(candidates, read_logins)
    warnings.extend(name_warnings)
    lines = render_map(logins)
    if lines != actual.map_lines:
        # The map goes first: a creation and a document of this plan give a
        # person a login name, and the map has to carry it by then.
        actions.append(WriteMap(lines=lines))

    # Without the entry that waits, the volume would hold no known entry once
    # the other removals pass, so a removal of an orphan that fails would
    # start a hold that tries nothing again.
    waiting: int | None = None
    if (
        not hold
        and known_uids.isdisjoint(members)
        and any(a.uid not in users_by_uid for a in counted)
    ):
        waiting = min(known_uids)

    held: set[int] = set()
    # A login closes again while its account still has a session, so the next
    # cycle ends a session that a failed stop left, or one that started while
    # the login closed.
    for account in counted:
        if account.uid in foreign_uids:
            if account.enabled or account.name in actual.session_accounts:
                actions.append(DisableAccount(account=account.name))
            warnings.append(
                f"samba entry '{account.name}' was made for another paperless user "
                f"than the user {account.user_id} of this database. Its login "
                f"is closed and nothing else changes. Point the daemon to the "
                f"paperless database of this entry, or start with empty volumes."
            )
            continue
        user = members.get(account.uid)
        if user is not None:
            # a member without a line keeps the account, the folder and the
            # workflow, and only the login name is gone
            actions.extend(
                _member_actions(
                    user,
                    account,
                    actual.unix_names,
                    actual.session_accounts,
                    folders,
                    workflows,
                    paperless_gid,
                )
            )
            continue
        if hold or account.uid == waiting:
            if account.enabled or account.name in actual.session_accounts:
                actions.append(DisableAccount(account=account.name))
            continue
        user_id = account.user_id
        # The plan takes the line of an account without a member out of the
        # map, before its removal and also in a hold, where nothing is
        # removed. So the login name of such an account comes from the map as
        # the cycle read it.
        login_name = read_logins.get(account.name)
        documents = _removal_documents(user_id, login_name, actual.documents, delivery)
        held.update(documents or ())
        workflow = workflows.get(account.name)
        folder = folders.get(account.name)
        actions.append(
            Deprovision(
                account=account.name,
                user_id=user_id,
                login_name=login_name,
                workflow_id=workflow.id if workflow else None,
                # a folder with another owner is not one that the daemon made
                # for this entry, so its scans stay
                delete_folder=folder is not None and folder.uid == account.uid,
                documents=documents,
            )
        )

    created: set[str] = set()
    for uid in sorted(members):
        account = account_name(uid)
        if account not in logins or uid in entry_uids:
            continue
        user = members[uid]
        # the cycle after this one finds the entry and makes the folder
        actions.append(CreateAccount(user=user, uid=uid, upload=delivery is not None))
        # a workflow that is left under the account name belongs to this
        # account
        actions.extend(_workflow_action(user, account, workflows))
        created.add(account)

    if hold_cause is not None:
        warnings.append(
            f"smb-sync is on hold: {hold_cause}. It creates no account and deletes "
            f"nothing. Point the daemon to the paperless database of the entries, or "
            f"start with empty volumes."
        )

    for entry in actual.accounts:
        if entry.username not in counted_names and entry.username not in created:
            warnings.append(
                f"samba entry '{entry.username}' is no account of smb-sync and was "
                f"left untouched. The username map gives it no login name, so it "
                f"cannot log in."
            )

    for folder in actual.folders:
        # a folder alone does not prove that the daemon made it, so it goes
        # only together with its account
        stopped = folder_stops.get(folder.name)
        if stopped is not None:
            warnings.append(
                f"user '{stopped.username}' gets no account: the folder "
                f"'{folder.name}' exists in the inbox and no account of smb-sync has "
                f"that name. Remove the folder."
            )
        elif folder.name not in counted_names:
            warnings.append(
                f"folder '{folder.name}' (uid {folder.uid}) belongs to no account "
                f"of smb-sync and was left untouched"
            )

    if actual.token_user is not None and delivery is None:
        warnings.append(NO_SUPERUSER_WARNING)

    # paperless imports a scan with or without a workflow, and the workflow
    # only sets the owner. So the workflow of a counted entry goes inside the
    # removal of that entry, after the login closed, and a scan that arrives
    # while the login of a user who left the group is still open still gets
    # that user as its owner.
    for workflow in actual.workflows:
        if workflow.name not in counted_names and workflow.name not in created:
            actions.append(DeleteWorkflow(workflow_id=workflow.id))

    if delivery is not None:
        users_with_line = {
            user.id: user
            for uid, user in members.items()
            if account_name(uid) in logins
        }
        member_ids = {user.id for user in members.values()}
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
            action = _document_action(document, delivery, users_with_line, member_ids)
            if action is not None:
                actions.append(action)

    if hold:
        actions = [
            a for a in actions if not isinstance(a, (DeleteDocument, DeleteWorkflow))
        ]
    return Plan(actions=tuple(actions), warnings=tuple(warnings), logins=logins)
