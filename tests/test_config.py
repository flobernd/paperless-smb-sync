import pytest

from smb_sync.config import config_warnings, load_config

BASE_ENV = {
    "PAPERLESS_URL": "http://paperless:8000",
    "PAPERLESS_API_TOKEN": "tok123",
}


def test_defaults():
    cfg = load_config(BASE_ENV)
    assert cfg.paperless_url == "http://paperless:8000"
    assert cfg.paperless_token == "tok123"
    assert cfg.sync_group == "smb"
    assert cfg.poll_interval == 30
    assert cfg.uid_base == 20000
    assert cfg.paperless_gid == 10000
    assert cfg.share_name == "inbox"
    assert cfg.smb_host == ""
    assert cfg.inbox_path == "/shares/inbox"


def test_url_trailing_slash_stripped():
    cfg = load_config({**BASE_ENV, "PAPERLESS_URL": "http://paperless:8000/"})
    assert cfg.paperless_url == "http://paperless:8000"


def test_overrides():
    cfg = load_config(
        {
            **BASE_ENV,
            "SYNC_GROUP": "scanner",
            "POLL_INTERVAL": "5",
            "UID_BASE": "30000",
            "PAPERLESS_GID": "12000",
            "SHARE_NAME": "scans",
            "SMB_HOST": "nas.lan",
            "INBOX_PATH": "/tmp/inbox",
        }
    )
    assert cfg.sync_group == "scanner"
    assert cfg.poll_interval == 5
    assert cfg.uid_base == 30000
    assert cfg.paperless_gid == 12000
    assert cfg.share_name == "scans"
    assert cfg.smb_host == "nas.lan"
    assert cfg.inbox_path == "/tmp/inbox"


def test_missing_url_raises():
    with pytest.raises(ValueError, match="PAPERLESS_URL"):
        load_config({"PAPERLESS_API_TOKEN": "tok"})


def test_missing_token_raises():
    with pytest.raises(ValueError, match="PAPERLESS_API_TOKEN"):
        load_config({"PAPERLESS_URL": "http://x"})


def test_token_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("filetok\n")
    cfg = load_config(
        {"PAPERLESS_URL": "http://x", "PAPERLESS_API_TOKEN_FILE": str(token_file)}
    )
    assert cfg.paperless_token == "filetok"


def test_mail_is_off_by_default():
    cfg = load_config(BASE_ENV)
    assert cfg.email_credentials is False
    assert cfg.mail_enabled is False


def test_mail_settings_come_from_the_paperless_variables():
    cfg = load_config(
        {
            **BASE_ENV,
            "EMAIL_CREDENTIALS": "true",
            "PAPERLESS_EMAIL_HOST": "smtp.lan",
            "PAPERLESS_EMAIL_PORT": "587",
            "PAPERLESS_EMAIL_HOST_USER": "paperless@example.com",
            "PAPERLESS_EMAIL_HOST_PASSWORD": "smtp-secret",
            "PAPERLESS_EMAIL_USE_TLS": "true",
            "PAPERLESS_EMAIL_FROM": "scans@example.com",
        }
    )
    assert cfg.mail_enabled is True
    assert cfg.mail.host == "smtp.lan"
    assert cfg.mail.port == 587
    assert cfg.mail.username == "paperless@example.com"
    assert cfg.mail.password == "smtp-secret"
    assert cfg.mail.use_tls is True
    assert cfg.mail.use_ssl is False
    assert cfg.mail.sender == "scans@example.com"


def test_sender_falls_back_to_the_smtp_login():
    cfg = load_config(
        {
            **BASE_ENV,
            "EMAIL_CREDENTIALS": "1",
            "PAPERLESS_EMAIL_HOST": "smtp.lan",
            "PAPERLESS_EMAIL_HOST_USER": "paperless@example.com",
        }
    )
    assert cfg.mail.sender == "paperless@example.com"
    assert cfg.mail.port == 25


def test_mail_stays_disabled_without_a_host():
    cfg = load_config({**BASE_ENV, "EMAIL_CREDENTIALS": "true"})
    assert cfg.email_credentials is True
    assert cfg.mail_enabled is False


def test_warns_when_enabled_without_a_mail_host():
    cfg = load_config({**BASE_ENV, "EMAIL_CREDENTIALS": "true"})
    warnings = config_warnings(cfg)
    assert len(warnings) == 1
    assert "PAPERLESS_EMAIL_HOST" in warnings[0]


def test_no_warning_when_disabled_or_fully_configured():
    assert config_warnings(load_config(BASE_ENV)) == ()
    configured = load_config(
        {**BASE_ENV, "EMAIL_CREDENTIALS": "true", "PAPERLESS_EMAIL_HOST": "smtp.lan"}
    )
    assert config_warnings(configured) == ()


def test_token_and_token_file_conflict(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("filetok")
    with pytest.raises(ValueError, match="only one"):
        load_config(
            {
                "PAPERLESS_URL": "http://x",
                "PAPERLESS_API_TOKEN": "tok",
                "PAPERLESS_API_TOKEN_FILE": str(token_file),
            }
        )
