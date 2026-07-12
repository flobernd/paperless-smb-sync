import argparse
import logging
import sys

from .config import Config, load_config
from .credentials import credentials_block, generate_password
from .model import UNMAPPED_UID, ActualState, PaperlessUser
from .paperless import PaperlessClient
from .service import run_cycle_locked, run_daemon
from .system import SystemAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smb-sync", description="Sync paperless-ngx users to Samba"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the sync daemon")
    sub.add_parser("sync", help="run one reconcile cycle now")
    sub.add_parser("status", help="show managed users")
    regen = sub.add_parser(
        "regen", help="set a new password for a user and print it once"
    )
    regen.add_argument("username")
    return parser


def format_status(
    desired: list[PaperlessUser], actual: ActualState, uid_base: int
) -> str:
    accounts = {a.username: a for a in actual.accounts}
    folders = {f.name for f in actual.folders}
    workflows = {w.username for w in actual.workflows}
    desired_names = {u.username for u in desired}

    rows = [("USERNAME", "UID", "SAMBA", "FOLDER", "WORKFLOW")]
    for user in sorted(desired, key=lambda u: u.username):
        account = accounts.get(user.username)
        if account is None:
            samba = "missing"
        elif account.enabled:
            samba = "enabled"
        else:
            samba = "disabled"
        rows.append(
            (
                user.username,
                str(uid_base + user.id),
                samba,
                "yes" if user.username in folders else "no",
                "yes" if user.username in workflows else "no",
            )
        )
    for account in sorted(actual.accounts, key=lambda a: a.username):
        if account.uid >= uid_base and account.username not in desired_names:
            # a sentinel-uid account matching no desired user is excluded from
            # the planner's stale set, so it is never removed automatically
            if account.uid == UNMAPPED_UID:
                samba = "stranded, needs manual cleanup"
            else:
                samba = "pending removal"
            rows.append(
                (
                    account.username,
                    str(account.uid),
                    samba,
                    "yes" if account.username in folders else "no",
                    "yes" if account.username in workflows else "no",
                )
            )

    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in rows
    )


def regen_password(cfg: Config, system: SystemAdapter, username: str) -> int:
    known = {
        account.username
        for account in system.list_samba_accounts()
        if account.uid >= cfg.uid_base
    }
    if username not in known:
        print(f"no managed samba account '{username}'", file=sys.stderr)
        return 1
    password = generate_password()
    system.set_samba_password(username, password, create=False)
    print(credentials_block(cfg.smb_host, cfg.share_name, username, password))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    args = build_parser().parse_args(argv)
    cfg = load_config()
    api = PaperlessClient(cfg.paperless_url, cfg.paperless_token)
    system = SystemAdapter(cfg.inbox_path, cfg.paperless_gid)

    if args.command == "run":
        run_daemon(cfg, api, system)
        return 0
    if args.command == "sync":
        return 0 if run_cycle_locked(cfg, api, system) else 1
    if args.command == "status":
        desired = api.fetch_desired_users(cfg.sync_group)
        actual = ActualState(
            accounts=tuple(system.list_samba_accounts()),
            folders=tuple(system.list_inbox_folders()),
            workflows=tuple(api.fetch_managed_workflows()),
        )
        print(format_status(desired, actual, cfg.uid_base))
        return 0
    if args.command == "regen":
        return regen_password(cfg, system, args.username)
    return 2
