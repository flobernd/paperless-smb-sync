import pytest

from smb_sync.config import load_config

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
