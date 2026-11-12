import argparse
import logging
import sys
import time
from functools import partial

from .config import Config, config_warnings, load_config
from .credentials import credentials_block, generate_password
from .mail import deliver_credentials, send_credentials
from .model import ActualState, PaperlessUser, Plan, RenameUser
from .paperless import PaperlessClient, PaperlessError
from .planner import counted_accounts, counted_uid, plan_cycle
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
    desired: list[PaperlessUser], actual: ActualState, plan: Plan, uid_base: int
) -> str:
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


def _email_of(api: PaperlessClient, cfg: Config, username: str) -> str:
    user = next(
        (u for u in api.fetch_desired_users(cfg.sync_group) if u.username == username),
        None,
    )
    return user.email if user is not None else ""


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
    if account is None or counted_uid(account, cfg.uid_base) is None:
        print(f"no managed samba account '{username}'", file=sys.stderr)
        return 1
    password = generate_password()
    system.set_samba_password(username, password, create=False)
    print(credentials_block(cfg.smb_host, cfg.share_name, username, password))
    if not cfg.mail_enabled:
        return 0
    try:
        email = _email_of(api, cfg, username)
    except PaperlessError as exc:
        # the password is already changed and printed; an unreachable API
        # costs the mail, not the command
        log.error("could not look up the address for '%s': %s", username, exc)
        return 0
    deliver_credentials(
        partial(send_credentials, cfg.mail),
        email,
        smb_host=cfg.smb_host,
        share_name=cfg.share_name,
        username=username,
        password=password,
    )
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
    api = PaperlessClient(cfg.paperless_url, cfg.paperless_token, cfg.consume_path)
    system = SystemAdapter(INBOX_ROOT, cfg.paperless_gid)

    if args.command == "check":
        return 0
    if args.command == "health":
        fresh = heartbeat_is_fresh(
            DEFAULT_HEARTBEAT_PATH, cfg.poll_interval, time.time()
        )
        return 0 if fresh else 1
    if args.command == "restore":
        try:
            failures = restore_unix_accounts(system, cfg.uid_base)
        except CommandError as exc:
            print(f"smb-sync: {exc}", file=sys.stderr)
            return 1
        return 0 if failures == 0 else 1
    # the start runs check and restore, and Docker runs health every 30 s, so
    # only the commands below log the warnings
    for warning in config_warnings(cfg):
        log.warning("%s", warning)
    if args.command == "run":
        run_daemon(cfg, api, system)
        return 0
    if args.command == "sync":
        return 0 if run_cycle_locked(cfg, api, system) else 1
    if args.command == "status":
        try:
            desired, actual = observe(cfg, api, system)
        except (CommandError, OSError, PaperlessError) as exc:
            print(f"smb-sync: {exc}", file=sys.stderr)
            return 1
        plan = plan_cycle(desired, actual, cfg.uid_base, cfg.consume_path)
        print(format_status(desired, actual, plan, cfg.uid_base))
        return 0
    if args.command == "regen":
        return regen_password(cfg, system, args.username, api)
    return 2
