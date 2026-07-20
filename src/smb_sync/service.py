import fcntl
import logging
import time
from functools import partial

from .config import Config
from .executor import execute
from .mail import send_credentials
from .model import ActualState
from .paperless import PaperlessClient, PaperlessError
from .planner import plan_cycle
from .system import CommandError, SystemAdapter

log = logging.getLogger(__name__)

DEFAULT_LOCK_PATH = "/run/smb-sync.lock"


def run_cycle(cfg: Config, api: PaperlessClient, system: SystemAdapter) -> bool:
    try:
        desired = api.fetch_desired_users(cfg.sync_group)
        workflows = api.fetch_managed_workflows()
    except PaperlessError as exc:
        # an unreachable API must never look like "no users"
        log.error("paperless API unavailable, skipping cycle: %s", exc)
        return False
    try:
        actual = ActualState(
            accounts=tuple(system.list_samba_accounts()),
            folders=tuple(system.list_inbox_folders()),
            workflows=tuple(workflows),
        )
    except (CommandError, OSError) as exc:
        log.error("failed to read local state, skipping cycle: %s", exc)
        return False

    result = plan_cycle(desired, actual, cfg.uid_base)
    for warning in result.warnings:
        log.warning("%s", warning)
    failures = execute(
        result.actions,
        system,
        api,
        smb_host=cfg.smb_host,
        share_name=cfg.share_name,
        deliver=partial(send_credentials, cfg.mail) if cfg.mail_enabled else None,
    )
    return failures == 0


def run_cycle_locked(
    cfg: Config,
    api: PaperlessClient,
    system: SystemAdapter,
    lock_path: str = DEFAULT_LOCK_PATH,
) -> bool:
    # serializes the daemon loop against docker-exec'd `smb-sync sync`
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return run_cycle(cfg, api, system)


def run_daemon(
    cfg: Config,
    api: PaperlessClient,
    system: SystemAdapter,
    lock_path: str = DEFAULT_LOCK_PATH,
) -> None:
    log.info(
        "sync loop started (interval %ds, group '%s')",
        cfg.poll_interval,
        cfg.sync_group,
    )
    while True:
        try:
            run_cycle_locked(cfg, api, system, lock_path=lock_path)
        except Exception:
            log.exception("cycle crashed")
        time.sleep(cfg.poll_interval)
