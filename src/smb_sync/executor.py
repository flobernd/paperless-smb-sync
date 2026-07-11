import logging
from collections.abc import Sequence

from .credentials import credentials_block, generate_password
from .model import (
    Action,
    CreateAccount,
    CreateFolder,
    CreateWorkflow,
    DeleteAccount,
    DeleteFolder,
    DeleteWorkflow,
    DisableAccount,
    EnableAccount,
    RenameUser,
)
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
) -> int:
    failures = 0
    for action in actions:
        try:
            _apply(action, system, api, smb_host=smb_host, share_name=share_name)
        except Exception as exc:
            failures += 1
            log.error("action %r failed: %s", action, exc)
    return failures


def _apply(
    action: Action,
    system: SystemAdapter,
    api: PaperlessClient,
    *,
    smb_host: str,
    share_name: str,
) -> None:
    match action:
        case CreateAccount(user=user, uid=uid):
            system.create_unix_user(user.username, uid)
            password = generate_password()
            system.set_samba_password(user.username, password, create=True)
            log.info("provisioned samba account '%s' (uid %d)", user.username, uid)
            print(
                credentials_block(smb_host, share_name, user.username, password),
                flush=True,
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
        case DeleteAccount(username=username):
            system.delete_samba_account(username)
            system.delete_unix_user(username)
            log.info("deleted samba account '%s'", username)
        case DeleteFolder(username=username):
            system.delete_inbox_folder(username)
            log.info("deleted inbox folder '%s' including contents", username)
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
