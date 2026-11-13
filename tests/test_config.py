import pytest

from smb_sync.config import load_config

BASE_ENV = {
    "PAPERLESS_URL": "http://paperless:8000",
    "PAPERLESS_API_TOKEN": "tok123",
}


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
        }
    )
    assert cfg.sync_group == "scanner"
    assert cfg.poll_interval == 5
    assert cfg.uid_base == 30000
    assert cfg.paperless_gid == 12000
    assert cfg.share_name == "scans"
    assert cfg.smb_host == "nas.lan"


def test_invalid_integer_names_the_variable():
    with pytest.raises(ValueError, match="POLL_INTERVAL"):
        load_config({**BASE_ENV, "POLL_INTERVAL": "soon"})


def test_poll_interval_must_be_one_or_more():
    with pytest.raises(ValueError, match="POLL_INTERVAL"):
        load_config({**BASE_ENV, "POLL_INTERVAL": "0"})
    assert load_config({**BASE_ENV, "POLL_INTERVAL": "1"}).poll_interval == 1


def test_uid_base_must_be_1000_or_more():
    with pytest.raises(ValueError, match="UID_BASE"):
        load_config({**BASE_ENV, "UID_BASE": "999"})
    assert load_config({**BASE_ENV, "UID_BASE": "1000"}).uid_base == 1000


def test_paperless_gid_must_be_1_or_more():
    with pytest.raises(ValueError, match="PAPERLESS_GID"):
        load_config({**BASE_ENV, "PAPERLESS_GID": "0"})
    assert load_config({**BASE_ENV, "PAPERLESS_GID": "1"}).paperless_gid == 1


def test_credentials_document_must_be_a_known_flag_value():
    with pytest.raises(ValueError, match="CREDENTIALS_DOCUMENT"):
        load_config({**BASE_ENV, "CREDENTIALS_DOCUMENT": "ture"})
    cfg = load_config({**BASE_ENV, "CREDENTIALS_DOCUMENT": "False"})
    assert cfg.credentials_document is False


def test_consume_path_must_be_a_plain_absolute_path():
    with pytest.raises(ValueError, match="PAPERLESS_CONSUMPTION_DIR"):
        load_config({**BASE_ENV, "PAPERLESS_CONSUMPTION_DIR": "../consume"})
    with pytest.raises(ValueError, match="PAPERLESS_CONSUMPTION_DIR"):
        load_config({**BASE_ENV, "PAPERLESS_CONSUMPTION_DIR": "/data/[scan]/consume"})
    with pytest.raises(ValueError, match="PAPERLESS_CONSUMPTION_DIR"):
        load_config({**BASE_ENV, "PAPERLESS_CONSUMPTION_DIR": "/data//consume"})
    with pytest.raises(ValueError, match="PAPERLESS_CONSUMPTION_DIR"):
        load_config({**BASE_ENV, "PAPERLESS_CONSUMPTION_DIR": "/data/x/../consume"})
    with pytest.raises(ValueError, match="PAPERLESS_CONSUMPTION_DIR"):
        load_config({**BASE_ENV, "PAPERLESS_CONSUMPTION_DIR": "//data/consume"})
    cfg = load_config({**BASE_ENV, "PAPERLESS_CONSUMPTION_DIR": "/data/consume/"})
    assert cfg.consume_path == "/data/consume"


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


def test_blank_token_raises(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("\n")
    with pytest.raises(ValueError, match="PAPERLESS_API_TOKEN"):
        load_config(
            {"PAPERLESS_URL": "http://x", "PAPERLESS_API_TOKEN_FILE": str(token_file)}
        )
    with pytest.raises(ValueError, match="PAPERLESS_API_TOKEN"):
        load_config({"PAPERLESS_URL": "http://x", "PAPERLESS_API_TOKEN": "   "})


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
