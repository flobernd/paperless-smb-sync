import logging
from collections.abc import Callable, Sequence
from functools import partial

from .credentials import credentials_block, generate_password
from .model import (
    Action,
    CreateAccount,
    CreateFolder,
    CreateWorkflow,
    DeleteDocument,
    DeleteWorkflow,
    Deprovision,
    DisableAccount,
    EnableAccount,
    HandOverDocument,
    PaperlessUser,
    RenameDocument,
    RepairFolder,
    RepairUnixAccount,
    UpdateWorkflow,
    WriteMap,
    account_name,
    map_logins,
)
from .paperless import PaperlessClient, PaperlessError
from .system import CommandError, SystemAdapter

log = logging.getLogger(__name__)


def execute(
    actions: Sequence[Action],
    system: SystemAdapter,
    api: PaperlessClient,
    *,
    smb_host: str,
    share_name: str,
) -> int:
    upload_credentials = partial(_upload_credentials, api, smb_host, share_name)
    failures = 0
    map_failed = False
    held = 0
    for action in actions:
        # Each of these three gives a person a login name: the block of a
        # creation, the title of a hand-over and the note of a rename. After a
        # failed map write the map does not carry that name yet, so they wait.
        # Each other action runs, so a login still closes.
        if map_failed and isinstance(
            action, (CreateAccount, HandOverDocument, RenameDocument)
        ):
            held += 1
            continue
        try:
            _apply(action, system, api, upload_credentials)
        except Exception as exc:
            failures += 1
            map_failed = map_failed or isinstance(action, WriteMap)
            # A CommandError names its command and a PaperlessError its
            # request, so one line shows the cause. Any other error needs its
            # traceback to be found.
            log.error(
                "action %r failed: %s",
                action,
                exc,
                exc_info=not isinstance(exc, (CommandError, PaperlessError)),
            )
    if held:
        log.warning(
            "the username map was not written, so each action that names a login "
            "waits for the next cycle (held back: %d)",
            held,
        )
    return failures


def _upload_credentials(
    api: PaperlessClient,
    smb_host: str,
    share_name: str,
    user: PaperlessUser,
    password: str,
) -> None:
    block = credentials_block(
        smb_host, share_name, user.username, password, for_terminal=False
    )
    try:
        api.upload_credentials(user.id, user.username, block)
        log.info("uploaded the credentials document of '%s'", user.username)
    except PaperlessError as exc:
        # A failed upload leaves a complete account whose password no person
        # has, and `smb-sync regen` gives it a new one, so the creation does
        # not fail.
        log.error(
            "could not upload the credentials document of '%s': %s. Run "
            "'smb-sync regen %s' to set a new password and show it.",
            user.username,
            exc,
            user.username,
        )


def _named(account: str, login_name: str | None) -> str:
    if login_name is None:
        return f"'{account}'"
    return f"'{account}' (login name '{login_name}')"


def _apply(
    action: Action,
    system: SystemAdapter,
    api: PaperlessClient,
    upload_credentials: Callable[[PaperlessUser, str], None],
) -> None:
    match action:
        case WriteMap(lines=lines):
            system.write_usermap(lines)
            log.info(
                "wrote the username map with %d login names", len(map_logins(lines))
            )
        case CreateAccount(user=user, uid=uid, upload=upload):
            account = account_name(uid)
            system.create_unix_user(account, uid)
            password = generate_password()
            system.set_samba_password(account, password, create=True)
            # smbpasswd -a makes the entry with an open login. The disable
            # comes before the mark, so a failed disable leaves an entry
            # without the mark, which the next cycle makes again.
            system.disable_samba_account(account)
            # An entry without the mark is one that the daemon does not
            # manage, so no person gets a password for it.
            system.store_mark(account, uid, user.joined)
            log.info(
                "provisioned samba account '%s' (uid %d) for the login name '%s'. "
                "Run 'smb-sync regen %s' to set a new password and show it.",
                account,
                uid,
                user.username,
                user.username,
            )
            if upload:
                upload_credentials(user, password)
        case RepairUnixAccount(account=account, uid=uid):
            system.create_unix_user(account, uid)
            log.warning(
                "recreated missing unix account '%s' (uid %d) from passdb",
                account,
                uid,
            )
        case CreateFolder(account=account, uid=uid):
            system.create_inbox_folder(account, uid)
            log.info("created inbox folder '%s'", account)
        case RepairFolder(account=account, uid=uid):
            system.repair_inbox_folder(account, uid)
            log.info("repaired the owner and the group of inbox folder '%s'", account)
        case CreateWorkflow(user_id=user_id, account=account):
            api.create_workflow(user_id, account)
            log.info("created consumption workflow for '%s'", account)
        case UpdateWorkflow(workflow_id=workflow_id, user_id=user_id, account=account):
            api.update_workflow(workflow_id, user_id, account)
            log.info("updated consumption workflow for '%s'", account)
        case DeleteWorkflow(workflow_id=workflow_id):
            api.delete_workflow(workflow_id)
            log.info("deleted workflow %d", workflow_id)
        case Deprovision(
            account=account,
            user_id=user_id,
            login_name=login_name,
            workflow_id=workflow_id,
            delete_folder=delete_folder,
            documents=documents,
        ):
            # A failed step ends the removal. The planner plans a removal only
            # from a counted entry, so the entry goes last, and while it exists
            # the next cycle tries the removal again. The retry still works
            # once the unix account is gone, since smbpasswd -d and pdbedit -x
            # need no unix account. The login closes first, so a removal that
            # stops half way leaves no open login.
            system.disable_samba_account(account)
            # The disabled flag goes first, so a client that connects again by
            # itself after the stop gets no new session.
            system.stop_sessions(account)
            if workflow_id is not None:
                api.delete_workflow(workflow_id)
                log.info("deleted workflow %d", workflow_id)
            if documents is not None:
                api.delete_credentials(user_id, documents)
                log.info(
                    "deleted the credentials documents of %s",
                    _named(account, login_name),
                )
            if delete_folder:
                system.delete_inbox_folder(account)
                log.info("deleted inbox folder '%s' including contents", account)
            system.delete_unix_user(account)
            system.delete_samba_account(account)
            log.info("deleted samba account %s", _named(account, login_name))
        case DisableAccount(account=account):
            system.disable_samba_account(account)
            # The disabled flag goes first, so a client that connects again by
            # itself after the stop gets no new session.
            system.stop_sessions(account)
            log.info("disabled samba account '%s'", account)
        case EnableAccount(account=account):
            system.enable_samba_account(account)
            log.info("re-enabled samba account '%s'", account)
        case DeleteDocument(document_id=document_id):
            api.delete_document(document_id)
            log.info("deleted credentials document %d", document_id)
        case HandOverDocument(
            document_id=document_id, user_id=user_id, username=username
        ):
            api.hand_over_document(document_id, user_id, username)
            log.info(
                "handed credentials document %d to user %d under the title for '%s'",
                document_id,
                user_id,
                username,
            )
        case RenameDocument(
            document_id=document_id, old_username=old, new_username=new
        ):
            # The title marks the rename as done, so it goes last, and each
            # cycle tries again until both steps are done. The note goes in
            # only when the document lacks it, so a retry adds no second one.
            api.ensure_document_note(
                document_id,
                f"The username changed from '{old}' to '{new}'. "
                f"The password is the same.",
            )
            api.retitle_document(document_id, new)
            log.info("retitled credentials document %d for '%s'", document_id, new)
