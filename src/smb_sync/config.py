import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    paperless_url: str
    paperless_token: str
    sync_group: str
    poll_interval: int
    uid_base: int
    paperless_gid: int
    share_name: str
    smb_host: str
    inbox_path: str


def _read_token(env: Mapping[str, str]) -> str:
    token = env.get("PAPERLESS_API_TOKEN", "")
    token_file = env.get("PAPERLESS_API_TOKEN_FILE", "")
    if token and token_file:
        raise ValueError(
            "set only one of PAPERLESS_API_TOKEN and PAPERLESS_API_TOKEN_FILE"
        )
    if token_file:
        with open(token_file, encoding="utf-8") as fh:
            return fh.read().strip()
    if token:
        return token
    raise ValueError("PAPERLESS_API_TOKEN or PAPERLESS_API_TOKEN_FILE is required")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    if env is None:
        env = os.environ
    url = env.get("PAPERLESS_URL", "").rstrip("/")
    if not url:
        raise ValueError("PAPERLESS_URL is required")
    return Config(
        paperless_url=url,
        paperless_token=_read_token(env),
        sync_group=env.get("SYNC_GROUP", "smb"),
        poll_interval=int(env.get("POLL_INTERVAL", "30")),
        uid_base=int(env.get("UID_BASE", "20000")),
        paperless_gid=int(env.get("PAPERLESS_GID", "10000")),
        share_name=env.get("SHARE_NAME", "inbox"),
        smb_host=env.get("SMB_HOST", ""),
        inbox_path=env.get("INBOX_PATH", "/shares/inbox"),
    )
