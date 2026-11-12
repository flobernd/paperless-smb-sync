import logging
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

from .config import MailConfig
from .credentials import credentials_block

log = logging.getLogger(__name__)

# a wedged SMTP server must not stall the sync loop
_TIMEOUT = 30

# (recipient, username, credentials block)
CredentialDelivery = Callable[[str, str, str], None]


class MailError(Exception):
    pass


def smtp_class(mail: MailConfig) -> type[smtplib.SMTP]:
    return smtplib.SMTP_SSL if mail.use_ssl else smtplib.SMTP


def _build_message(mail: MailConfig, to: str, username: str, block: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"SMB scan inbox credentials for '{username}'"
    message["From"] = mail.sender
    message["To"] = to
    message.set_content(
        f"Your scan inbox is ready. Enter these details in your scanner:\n\n{block}\n"
    )
    return message


def send_credentials(
    mail: MailConfig,
    to: str,
    username: str,
    block: str,
    *,
    smtp_factory: Callable[..., Any] | None = None,
) -> None:
    factory = smtp_factory or smtp_class(mail)
    message = _build_message(mail, to, username, block)
    try:
        with factory(mail.host, mail.port, timeout=_TIMEOUT) as smtp:
            # starttls on an implicit-TLS connection is a protocol error
            if mail.use_tls and not mail.use_ssl:
                smtp.starttls()
            if mail.username:
                smtp.login(mail.username, mail.password)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"sending to {to} failed: {exc}") from exc


def deliver_credentials(
    deliver: CredentialDelivery,
    email: str,
    *,
    smb_host: str,
    share_name: str,
    username: str,
    password: str,
) -> None:
    if not email:
        log.warning(
            "user '%s' has no email address in paperless; credentials were only "
            "printed to the log",
            username,
        )
        return
    block = credentials_block(smb_host, share_name, username, password, for_log=False)
    try:
        deliver(email, username, block)
        log.info("emailed credentials for '%s' to %s", username, email)
    except MailError as exc:
        # the password is already set and the block is in the log, so a dead
        # mail server must not make the caller treat this as a failure
        log.error("could not email credentials for '%s': %s", username, exc)
