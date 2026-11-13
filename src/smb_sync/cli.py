import argparse
import logging
import sys
import time

from .config import Config, load_config
from .credentials import credentials_block, generate_password
from .model import ActualState, PaperlessUser, Plan, RenameUser
from .paperless import PaperlessClient, PaperlessError
from .planner import (
    NO_SUPERUSER_WARNING,
    counted_accounts,
    counted_uid,
    plan_cycle,
    user_documents,
)
from .service import (
    DEFAULT_HEARTBEAT_PATH,
    heartbeat_is_fresh,
    observe,
    restore_unix_accounts,
    run_cycle_locked,
    run_daemon,
)
from .system import INBOX_ROOT, CommandError, SystemAdapter

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smb-sync", description="Sync paperless-ngx users to Samba"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="check the configuration")
    sub.add_parser("health", help="check that the sync loop finished a cycle recently")
    regen = sub.add_parser(
        "regen", help="set a new password for a user and print it once"
    )
    regen.add_argument("username")
    sub.add_parser(
        "restore", help="make the unix accounts of the managed samba entries"
    )
    sub.add_parser("run", help="run the sync daemon")
    sub.add_parser("status", help="show managed users")
    sub.add_parser("sync", help="run one reconcile cycle now")
    return parser


def format_status(
    users: list[PaperlessUser], actual: ActualState, plan: Plan, uid_base: int
) -> str:
    desired = [u for u in users if u.member]
    # the planner ignores an entry that does not count, so its user reads as
    # missing
    accounts = {a.username: a for a in counted_accounts(actual.accounts, uid_base)}
    folders = {f.name for f in actual.folders}
    workflows = {w.username for w in actual.workflows}
    desired_names = {u.username for u in desired}
    renamed_to = {
        a.old_username: a.new_username
        for a in plan.actions
        if isinstance(a, RenameUser)
    }

    def status_row(username: str, uid: int | None, samba: str) -> tuple[str, ...]:
        return (
            username,
            "-" if uid is None else str(uid),
            samba,
            "yes" if username in folders else "no",
            "yes" if username in workflows else "no",
        )

    rows: list[tuple[str, ...]] = [("USERNAME", "UID", "SAMBA", "FOLDER", "WORKFLOW")]
    for user in sorted(desired, key=lambda u: u.username):
        account = accounts.get(user.username)
        if account is None:
            samba = "missing"
        elif account.enabled:
            samba = "enabled"
        else:
            samba = "disabled"
        rows.append(status_row(user.username, uid_base + user.id, samba))
    for account in sorted(actual.accounts, key=lambda a: a.username):
        if account.username in desired_names:
            continue
        if account.username in renamed_to:
            samba = f"renaming to {renamed_to[account.username]}"
        elif account.username in plan.pending_removal:
            samba = "pending removal"
        else:
            samba = "left untouched"
        rows.append(status_row(account.username, account.uid, samba))

    widths = [max(map(len, column)) for column in zip(*rows)]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
        for row in rows
    ]
    if plan.warnings:
        lines += ["", "Warnings:", *(f"  {warning}" for warning in plan.warnings)]
    return "\n".join(lines)


def _update_document(
    cfg: Config, api: PaperlessClient, username: str, user_id: int, password: str
) -> None:
    block = credentials_block(
        cfg.smb_host, cfg.share_name, username, password, for_log=False
    )
    try:
        token_user = api.token_user()
        if not token_user.is_superuser:
            log.warning("%s", NO_SUPERUSER_WARNING)
            return
        documents = user_documents(
            api.fetch_credential_documents(), user_id, token_user
        )
        target = next(iter(documents), None)
        if target is None:
            api.upload_credentials(user_id, username, block)
            log.info("uploaded a new credentials document for '%s'", username)
        else:
            # The owner can have granted a right to someone else, or given away
            # a document that it titled like this one, so the rights are reset
            # before the new password goes in.
            api.hand_over_document(target.id, user_id, username)
            api.add_credentials_version(target.id, block)
            log.info("added the new password to document %d", target.id)
    except PaperlessError as exc:
        # the password is already changed and printed, so a paperless error
        # must not fail the command
        log.error(
            "could not update the credentials document of '%s': %s", username, exc
        )


def regen_password(
    cfg: Config,
    system: SystemAdapter,
    username: str,
    api: PaperlessClient,
) -> int:
    account = next(
        (a for a in system.list_samba_accounts() if a.username == username),
        None,
    )
    if account is not None and account.uid is None:
        print(
            f"samba entry '{username}' stores no uid of the managed range, so "
            f"smb-sync does not manage it. If '{username}' is a member of the "
            f"sync group, the next cycle or 'smb-sync sync' makes the account "
            f"again and prints new credentials.",
            file=sys.stderr,
        )
        return 1
    stored_uid = None if account is None else counted_uid(account, cfg.uid_base)
    if account is None or stored_uid is None:
        print(f"no managed samba account '{username}'", file=sys.stderr)
        return 1
    password = generate_password()
    system.set_samba_password(username, password, create=False)
    if not account.enabled:
        # smbpasswd clears the disabled flag of an entry when it sets the
        # password, which would open the login of an inactive user
        system.disable_samba_account(username)
    print(credentials_block(cfg.smb_host, cfg.share_name, username, password))
    if cfg.credentials_document:
        _update_document(cfg, api, username, stored_uid - cfg.uid_base, password)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config()
    except (OSError, ValueError) as exc:
        print(f"smb-sync: {exc}", file=sys.stderr)
        return 2

    if args.command == "check":
        return 0
    if args.command == "health":
        fresh = heartbeat_is_fresh(
            DEFAULT_HEARTBEAT_PATH, cfg.poll_interval, time.time()
        )
        return 0 if fresh else 1

    api = PaperlessClient(cfg.paperless_url, cfg.paperless_token, cfg.consume_path)
    system = SystemAdapter(INBOX_ROOT, cfg.paperless_gid)
    if args.command == "restore":
        try:
            failures = restore_unix_accounts(system, cfg.uid_base)
        except CommandError as exc:
            print(f"smb-sync: {exc}", file=sys.stderr)
            return 1
        return 0 if failures == 0 else 1
    if args.command == "run":
        run_daemon(cfg, api, system)
        return 0
    if args.command == "sync":
        return 0 if run_cycle_locked(cfg, api, system) else 1
    if args.command == "status":
        try:
            users, actual = observe(cfg, api, system)
        except (CommandError, OSError, PaperlessError) as exc:
            print(f"smb-sync: {exc}", file=sys.stderr)
            return 1
        plan = plan_cycle(users, actual, cfg.uid_base, cfg.consume_path)
        print(format_status(users, actual, plan, cfg.uid_base))
        return 0
    return regen_password(cfg, system, args.username, api)
