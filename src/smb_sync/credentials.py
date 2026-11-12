import secrets
import string

# letters and digits alone, since symbols are painful to type on a scanner keypad
_ALPHABET = string.ascii_letters + string.digits
PASSWORD_LENGTH = 20


def generate_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(PASSWORD_LENGTH))


def credentials_block(
    host: str,
    share_name: str,
    username: str,
    password: str,
    *,
    for_log: bool = True,
) -> str:
    server = host or "<server>"
    lines = [
        "=" * 60,
        f"SMB credentials for '{username}'",
        f"  Server:   {server}",
        f"  Share:    \\\\{server}\\{share_name}",
        f"  Username: {username}",
        f"  Password: {password}",
    ]
    # The document keeps the block, and `smb-sync regen` runs inside the
    # container, so both lines hold for a reader of the log alone.
    if for_log:
        lines.append("This password is shown only once.")
        lines.append(f"Regenerate with: smb-sync regen {username}")
    lines.append("=" * 60)
    return "\n".join(lines)
