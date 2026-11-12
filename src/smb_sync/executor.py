import logging
from collections.abc import Callable, Sequence
from functools import partial

from .credentials import credentials_block, generate_password
from .model import (
    Action,
    CreateAccount,
    CreateFolder,
    CreateWorkflow,
    DeleteWorkflow,
    Deprovision,
    DisableAccount,
    EnableAccount,
    PaperlessUser,
    RenameUser,
    RepairUnixAccount,
)
from .mail import CredentialDelivery, deliver_credentials
from .paperless import PaperlessClient
from .system import SystemAdapter

log = logging.getLogger(__name__)


def execute(
    actions: Sequence[Action],
    system: SystemAdapter,
    api: PaperlessClient,
    *,
    smb_host: str,
    share_name: str,
    deliver: CredentialDelivery | None,
) -> int:
    announce = partial(_announce_credentials, smb_host, share_name, deliver)
    failures = 0
    for action in actions:
        try:
            _apply(action, system, api, announce)
        except Exception as exc:
            failures += 1
            # exc_info so unexpected failures in a service that deletes
            # accounts and folders stay diagnosable from the logs
            log.error("action %r failed: %s", action, exc, exc_info=True)
    return failures


def _announce_credentials(
    smb_host: str,
    share_name: str,
    deliver: CredentialDelivery | None,
    user: PaperlessUser,
    password: str,
) -> None:
    print(credentials_block(smb_host, share_name, user.username, password), flush=True)
    if deliver is not None:
        deliver_credentials(
            deliver,
            user.email,
            smb_host=smb_host,
            share_name=share_name,
            username=user.username,
            password=password,
        )


def _apply(
    action: Action,
    system: SystemAdapter,
    api: PaperlessClient,
    announce: Callable[[PaperlessUser, str], None],
) -> None:
    match action:
        case CreateAccount(user=user, uid=uid):
            system.create_unix_user(user.username, uid)
            password = generate_password()
            system.set_samba_password(user.username, password, create=True)
            log.info("provisioned samba account '%s' (uid %d)", user.username, uid)
            announce(user, password)
            # the stored uid marks the creation as finished, so it waits until
            # the password went out
            system.store_uid(user.username, uid)
        case RepairUnixAccount(username=username, uid=uid):
            system.create_unix_user(username, uid)
            log.warning(
                "recreated missing unix account '%s' (uid %d) from passdb",
                username,
                uid,
            )
        case CreateFolder(username=username, uid=uid):
            system.create_inbox_folder(username, uid)
            log.info("created inbox folder '%s'", username)
        case CreateWorkflow(user_id=user_id, username=username):
            api.create_workflow(user_id, username)
            log.info("created consumption workflow for '%s'", username)
        case DeleteWorkflow(workflow_id=workflow_id):
            api.delete_workflow(workflow_id)
            log.info("deleted workflow %d", workflow_id)
        case Deprovision(
            username=username, workflow_id=workflow_id, delete_folder=delete_folder
        ):
            # A failed step ends the removal. The entry goes after the folder
            # because it proves that the daemon made the folder, and while it
            # exists the next cycle tries the removal again. The login closes
            # first, so a removal that stops half way leaves no open login.
            system.disable_samba_account(username)
            if workflow_id is not None:
                api.delete_workflow(workflow_id)
                log.info("deleted workflow %d", workflow_id)
            if delete_folder:
                system.delete_inbox_folder(username)
                log.info("deleted inbox folder '%s' including contents", username)
            system.delete_samba_account(username)
            system.delete_unix_user(username)
            log.info("deleted samba account '%s'", username)
        case DisableAccount(username=username):
            system.disable_samba_account(username)
            log.info("disabled samba account '%s' (user inactive)", username)
        case EnableAccount(username=username):
            system.enable_samba_account(username)
            log.info("re-enabled samba account '%s'", username)
        case RenameUser(
            old_username=old,
            new_username=new,
            uid=uid,
            user_id=user_id,
            workflow_id=workflow_id,
        ):
            system.rename_samba_account(old, new, uid)
            system.rename_inbox_folder(old, new)
            if workflow_id is not None:
                api.rename_workflow(workflow_id, user_id, new)
            log.warning(
                "renamed '%s' to '%s'; update the scanner profile folder path "
                "manually",
                old,
                new,
            )
