import smtplib

import pytest

from smb_sync.config import MailConfig
from smb_sync.mail import MailError, send_credentials, smtp_class

BLOCK = "SMB credentials for 'carol'\n  Password: secret123"


def mail_config(**overrides):
    base = {
        "host": "smtp.lan",
        "port": 587,
        "username": "",
        "password": "",
        "use_tls": False,
        "use_ssl": False,
        "sender": "scans@example.com",
    }
    return MailConfig(**{**base, **overrides})


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.sent = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.sent.append(message)


def recording_factory(created, raises=None):
    def factory(host, port, timeout=None):
        if raises is not None:
            raise raises
        smtp = FakeSMTP(host, port, timeout)
        created.append(smtp)
        return smtp

    return factory


def test_sends_the_block_to_the_user():
    created = []
    send_credentials(
        mail_config(),
        "carol@example.com",
        "carol",
        BLOCK,
        smtp_factory=recording_factory(created),
    )
    smtp = created[0]
    assert (smtp.host, smtp.port) == ("smtp.lan", 587)
    assert smtp.timeout is not None
    message = smtp.sent[0]
    assert message["To"] == "carol@example.com"
    assert message["From"] == "scans@example.com"
    assert "carol" in message["Subject"]
    assert BLOCK in message.get_content()
    assert smtp.closed is True


def test_starttls_when_configured():
    created = []
    send_credentials(
        mail_config(use_tls=True),
        "carol@example.com",
        "carol",
        BLOCK,
        smtp_factory=recording_factory(created),
    )
    assert created[0].started_tls is True


def test_no_starttls_on_an_implicit_ssl_connection():
    created = []
    send_credentials(
        mail_config(use_ssl=True, use_tls=True),
        "carol@example.com",
        "carol",
        BLOCK,
        smtp_factory=recording_factory(created),
    )
    assert created[0].started_tls is False


def test_logs_in_only_when_a_username_is_set():
    created = []
    send_credentials(
        mail_config(username="paperless", password="pw"),
        "carol@example.com",
        "carol",
        BLOCK,
        smtp_factory=recording_factory(created),
    )
    assert created[0].login_args == ("paperless", "pw")

    anonymous = []
    send_credentials(
        mail_config(),
        "carol@example.com",
        "carol",
        BLOCK,
        smtp_factory=recording_factory(anonymous),
    )
    assert anonymous[0].login_args is None


def test_os_errors_are_wrapped():
    factory = recording_factory([], raises=ConnectionRefusedError("refused"))
    with pytest.raises(MailError, match="refused"):
        send_credentials(mail_config(), "carol@example.com", "carol", BLOCK, smtp_factory=factory)


def test_ssl_selects_the_implicit_tls_transport():
    assert smtp_class(mail_config(use_ssl=True)) is smtplib.SMTP_SSL
    assert smtp_class(mail_config()) is smtplib.SMTP
