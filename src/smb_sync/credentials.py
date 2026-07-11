import secrets
import string

# letters and digits only; symbols are painful to type on scanner keypads
_ALPHABET = string.ascii_letters + string.digits
PASSWORD_LENGTH = 20


def generate_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(PASSWORD_LENGTH))


def credentials_block(
    host: str, share_name: str, username: str, password: str
) -> str:
    server = host or "<server>"
    return "\n".join(
        [
            "=" * 60,
            f"SMB credentials for '{username}'",
            f"  Server:   {server}",
            f"  Share:    \\\\{server}\\{share_name}",
            f"  Username: {username}",
            f"  Password: {password}",
            "This password is shown only once.",
            f"Regenerate with: smb-sync regen {username}",
            "=" * 60,
        ]
    )
