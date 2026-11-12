import fcntl
import logging
import os
import time
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path

from .config import Config
from .executor import execute
from .mail import send_credentials
from .model import ActualState, CredentialDocument, PaperlessUser, TokenUser
from .paperless import PaperlessClient, PaperlessError
from .planner import counted_uid, plan_cycle
from .system import CommandError, SystemAdapter

log = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_PATH = "/run/smb-sync.heartbeat"
DEFAULT_LOCK_PATH = "/run/smb-sync.lock"


class WarningRepeat:
    def __init__(
        self,
        interval: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._logged_at: dict[str, float] = {}

    def due(self, warnings: Sequence[str]) -> list[str]:
        now = self._clock()
        # forgetting absent warnings keeps those of departed users from piling
        # up, and a warning that comes back is logged at once
        logged_at = {w: t for w, t in self._logged_at.items() if w in warnings}
        due = []
        for warning in warnings:
            last = logged_at.get(warning)
            if last is None or now - last >= self._interval:
                logged_at[warning] = now
                due.append(warning)
        self._logged_at = logged_at
        return due


def _observe_documents(
    api: PaperlessClient,
) -> tuple[TokenUser | None, tuple[CredentialDocument, ...]]:
    try:
        token_user = api.token_user()
        if not token_user.is_superuser:
            return token_user, ()
        return token_user, tuple(api.fetch_credential_documents())
    except PaperlessError as exc:
        # the documents are optional, so a failed read must not hold back the
        # creation and the removal of accounts
        log.error("credentials documents are unavailable in this cycle: %s", exc)
        return None, ()


def observe(
    cfg: Config, api: PaperlessClient, system: SystemAdapter
) -> tuple[list[PaperlessUser], ActualState]:
    desired = api.fetch_desired_users(cfg.sync_group)
    workflows = api.fetch_managed_workflows()
    token_user, documents = (
        _observe_documents(api) if cfg.credentials_document else (None, ())
    )
    actual = ActualState(
        accounts=tuple(system.list_samba_accounts()),
        folders=tuple(system.list_inbox_folders()),
        workflows=tuple(workflows),
        unix_uids=system.list_unix_uids(),
        documents=documents,
        token_user=token_user,
    )
    return desired, actual


def run_cycle(
    cfg: Config,
    api: PaperlessClient,
    system: SystemAdapter,
    repeat: WarningRepeat | None = None,
) -> bool:
    try:
        desired, actual = observe(cfg, api, system)
    except PaperlessError as exc:
        # an unreachable API must never look like "no users"
        log.error("paperless API unavailable, skipping cycle: %s", exc)
        return False
    except (CommandError, OSError) as exc:
        log.error("failed to read local state, skipping cycle: %s", exc)
        return False

    result = plan_cycle(desired, actual, cfg.uid_base, cfg.consume_path)
    warnings = result.warnings if repeat is None else repeat.due(result.warnings)
    for warning in warnings:
        log.warning("%s", warning)
    failures = execute(
        result.actions,
        system,
        api,
        smb_host=cfg.smb_host,
        share_name=cfg.share_name,
        deliver=partial(send_credentials, cfg.mail) if cfg.mail_enabled else None,
        upload=actual.token_user is not None and actual.token_user.is_superuser,
    )
    return failures == 0


def run_cycle_locked(
    cfg: Config,
    api: PaperlessClient,
    system: SystemAdapter,
    lock_path: str = DEFAULT_LOCK_PATH,
    repeat: WarningRepeat | None = None,
) -> bool:
    # serializes the daemon loop against docker-exec'd `smb-sync sync`
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return run_cycle(cfg, api, system, repeat=repeat)


def heartbeat_is_fresh(path: str, poll_interval: int, now: float) -> bool:
    try:
        modified = os.stat(path).st_mtime
    except FileNotFoundError:
        return False
    # each request to a paperless that hangs waits 30 s, which can outlast
    # three short poll intervals
    return now - modified <= max(3 * poll_interval, 180)


def run_daemon(
    cfg: Config,
    api: PaperlessClient,
    system: SystemAdapter,
    lock_path: str = DEFAULT_LOCK_PATH,
    heartbeat_path: str = DEFAULT_HEARTBEAT_PATH,
) -> None:
    log.info(
        "sync loop started (interval %ds, group '%s')",
        cfg.poll_interval,
        cfg.sync_group,
    )
    repeat = WarningRepeat()
    # after docker restart, the file of the earlier run would read fresh while
    # the first cycle hangs
    try:
        Path(heartbeat_path).unlink(missing_ok=True)
    except OSError as exc:
        log.error("could not remove the heartbeat file '%s': %s", heartbeat_path, exc)
    while True:
        try:
            run_cycle_locked(cfg, api, system, lock_path=lock_path, repeat=repeat)
        except Exception:
            log.exception("cycle crashed")
        # the heartbeat follows a failed cycle too, so an unreachable paperless
        # does not mark the container unhealthy
        try:
            Path(heartbeat_path).touch()
        except OSError as exc:
            log.error(
                "could not write the heartbeat file '%s': %s", heartbeat_path, exc
            )
        time.sleep(cfg.poll_interval)


def restore_unix_accounts(system: SystemAdapter, uid_base: int) -> int:
    failures = 0
    for account in system.list_samba_accounts():
        uid = counted_uid(account, uid_base)
        if uid is None:
            continue
        # a rename that stopped before its last step leaves two entries with
        # one uid, and useradd refuses the second, so one failure must not
        # lock out the entries after it
        try:
            system.create_unix_user(account.username, uid)
        except CommandError as exc:
            log.error(
                "could not restore the unix account of '%s': %s", account.username, exc
            )
            failures += 1
    return failures
