import os
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    use_ssl: bool
    sender: str


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
    consume_path: str
    email_credentials: bool
    mail: MailConfig

    @property
    def mail_enabled(self) -> bool:
        return self.email_credentials and bool(self.mail.host)


def _read_token(env: Mapping[str, str]) -> str:
    token = env.get("PAPERLESS_API_TOKEN", "").strip()
    token_file = env.get("PAPERLESS_API_TOKEN_FILE", "")
    if token and token_file:
        raise ValueError(
            "set only one of PAPERLESS_API_TOKEN and PAPERLESS_API_TOKEN_FILE"
        )
    if token_file:
        with open(token_file, encoding="utf-8") as fh:
            token = fh.read().strip()
        if not token:
            raise ValueError("PAPERLESS_API_TOKEN_FILE holds no token")
        return token
    if token:
        return token
    raise ValueError("PAPERLESS_API_TOKEN or PAPERLESS_API_TOKEN_FILE is required")


def _read_bool(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _read_int(
    env: Mapping[str, str], name: str, default: int, minimum: int | None = None
) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be {minimum} or more, got {value}")
    return value


def _read_consume_path(env: Mapping[str, str]) -> str:
    raw = env.get("PAPERLESS_CONSUMPTION_DIR", "/usr/src/paperless/consume")
    path = raw.rstrip("/")
    # The path starts the filter of each workflow, which paperless matches as
    # an fnmatch pattern against the absolute path of a scan.
    if not path.startswith("/"):
        raise ValueError(
            f"PAPERLESS_CONSUMPTION_DIR must be an absolute path, got '{raw}'"
        )
    if any(char in path for char in "*?[]"):
        raise ValueError(
            "PAPERLESS_CONSUMPTION_DIR must not contain the pattern characters "
            f"*, ?, [ and ], got '{raw}'"
        )
    # paperless resolves its consumption directory and the path of each scan,
    # so a filter with such parts matches no scan. The daemon cannot normalize
    # the value for paperless: a .. after a symbolic link leads to another
    # place than the text says. normpath keeps a leading // that paperless
    # drops, so that case has a check of its own.
    if path.startswith("//") or posixpath.normpath(path) != path:
        raise ValueError(
            "PAPERLESS_CONSUMPTION_DIR must not contain '//', '.' or '..' parts, "
            f"got '{raw}'"
        )
    return path


def _read_mail(env: Mapping[str, str]) -> MailConfig:
    # deliberately the paperless-ngx variable names, so an existing mail setup
    # can be shared with this container instead of configured twice
    login = env.get("PAPERLESS_EMAIL_HOST_USER", "")
    return MailConfig(
        host=env.get("PAPERLESS_EMAIL_HOST", ""),
        port=_read_int(env, "PAPERLESS_EMAIL_PORT", 25),
        username=login,
        password=env.get("PAPERLESS_EMAIL_HOST_PASSWORD", ""),
        use_tls=_read_bool(env, "PAPERLESS_EMAIL_USE_TLS"),
        use_ssl=_read_bool(env, "PAPERLESS_EMAIL_USE_SSL"),
        sender=env.get("PAPERLESS_EMAIL_FROM", "") or login or "smb-sync@localhost",
    )


def config_warnings(cfg: Config) -> tuple[str, ...]:
    if cfg.email_credentials and not cfg.mail.host:
        return (
            "EMAIL_CREDENTIALS is enabled but PAPERLESS_EMAIL_HOST is not set; "
            "credentials will only be printed to the log",
        )
    return ()


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
        poll_interval=_read_int(env, "POLL_INTERVAL", 30, minimum=1),
        uid_base=_read_int(env, "UID_BASE", 20000),
        paperless_gid=_read_int(env, "PAPERLESS_GID", 10000),
        share_name=env.get("SHARE_NAME", "inbox"),
        smb_host=env.get("SMB_HOST", ""),
        consume_path=_read_consume_path(env),
        email_credentials=_read_bool(env, "EMAIL_CREDENTIALS"),
        mail=_read_mail(env),
    )
