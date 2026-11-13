import argparse
import logging
import sys
import time

from .config import Config, load_config
from .credentials import credentials_block, generate_password
from .model import (
    ActualState,
    Deprovision,
    PaperlessUser,
    Plan,
    account_name,
    login_key,
    map_logins,
)
from .paperless import PaperlessClient, PaperlessError
from .planner import (
    NO_SUPERUSER_WARNING,
    counted_accounts,
    plan_cycle,
    user_documents,
)
from .service import (
    DEFAULT_HEARTBEAT_PATH,
    DEFAULT_LOCK_PATH,
    cycle_lock,
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
    regen.add_argument("login_name")
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
    members = {uid_base + u.id: u for u in users if u.member}
    entries = {a.uid: a for a in counted_accounts(actual.accounts, uid_base)}
    # the plan drops the line of an account that it removes, so the name of
    # such an account comes from the map as the cycle read it
    read_logins = map_logins(actual.map_lines)
    folders = {f.name for f in actual.folders}
    workflows = {w.name for w in actual.workflows}
    removals = {a.account for a in plan.actions if isinstance(a, Deprovision)}

    rows: list[tuple[str, ...]] = [
        ("LOGIN NAME", "ACCOUNT", "UID", "LOGIN", "FOLDER", "WORKFLOW")
    ]
    for uid in sorted(members.keys() | entries.keys()):
        account = account_name(uid)
        member = members.get(uid)
        entry = entries.get(uid)
        if account in removals:
            login = "pending removal"
        elif entry is None:
            login = "missing"
        elif account not in plan.logins:
            login = "no login name"
        elif entry.enabled:
            login = "open"
        else:
            login = "closed"
        rows.append(
            (
                read_logins.get(account, "-") if member is None else member.username,
                account,
                str(uid),
                login,
                "yes" if account in folders else "no",
                "yes" if account in workflows else "no",
            )
        )

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
    login_name: str,
    api: PaperlessClient,
    lock_path: str,
) -> int:
    # A cycle can remove or close the account between two steps, so the lock
    # is held from the map read to the document.
    with cycle_lock(lock_path):
        typed = login_key(login_name)
        line = next(
            (
                (account, login)
                for account, login in map_logins(system.read_usermap() or ()).items()
                if login_key(login) == typed
            ),
            None,
        )
        entries = counted_accounts(system.list_samba_accounts(), cfg.uid_base)
        entry = None
        if line is not None:
            entry = next((e for e in entries if e.name == line[0]), None)
        if line is None or entry is None:
            print(
                f"no managed account for the login name '{login_name}'",
                file=sys.stderr,
            )
            return 1
        account, login = line
        password = generate_password()
        system.set_samba_password(account, password, create=False)
        if not entry.enabled:
            # smbpasswd clears the disabled flag of an entry when it sets the
            # password, which would open the login of an inactive user
            system.disable_samba_account(account)
        print(credentials_block(cfg.smb_host, cfg.share_name, login, password))
        if cfg.credentials_document:
            _update_document(cfg, api, login, entry.uid - cfg.uid_base, password)
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
        plan = plan_cycle(users, actual, cfg.uid_base)
        print(format_status(users, actual, plan, cfg.uid_base))
        return 0
    return regen_password(cfg, system, args.login_name, api, DEFAULT_LOCK_PATH)
