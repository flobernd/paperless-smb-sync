import secrets
import string

# letters and digits only; symbols are painful to type on scanner keypads
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
    regen_hint: bool = True,
) -> str:
    server = host or "<server>"
    lines = [
        "=" * 60,
        f"SMB credentials for '{username}'",
        f"  Server:   {server}",
        f"  Share:    \\\\{server}\\{share_name}",
        f"  Username: {username}",
        f"  Password: {password}",
        "This password is shown only once.",
    ]
    # `smb-sync regen` runs inside the container, so it is only actionable for
    # whoever reads the log, not for a user who receives the block by mail
    if regen_hint:
        lines.append(f"Regenerate with: smb-sync regen {username}")
    lines.append("=" * 60)
    return "\n".join(lines)
