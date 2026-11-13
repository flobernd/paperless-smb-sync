import os
import pwd
import shutil
import subprocess
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .model import InboxFolder, SambaAccount, entry_mark

# The entrypoint, the share template, the smoke test and the volume mounts of
# the two compose files use the same path, so they change together with this
# constant.
INBOX_ROOT = "/shares/inbox"

# The volume that holds passdb, so the map survives a recreation of the
# container together with the entries that its lines name.
STATE_DIR = "/var/lib/samba"

_USERMAP = "smb-sync-usermap"


class CommandError(Exception):
    pass


@contextmanager
def _command_error(what: str) -> Generator[None, None, None]:
    try:
        yield
    except OSError as exc:
        raise CommandError(f"{what}: {exc}") from exc


def _unix_user(username: str) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwnam(username)
    except KeyError:
        return None


def _run(argv: list[str], input_text: str | None = None) -> str:
    with _command_error(f"cannot run {argv[0]}"):
        result = subprocess.run(argv, input=input_text, capture_output=True, text=True)
    if result.returncode != 0:
        raise CommandError(
            f"{' '.join(argv)} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


class SystemAdapter:
    def __init__(
        self,
        inbox_path: str,
        paperless_gid: int,
        run: Callable[..., str] | None = None,
        state_dir: str = STATE_DIR,
    ):
        self._inbox = Path(inbox_path)
        self._gid = paperless_gid
        self._run = run or _run
        self._usermap = Path(state_dir) / _USERMAP

    # -- observation -------------------------------------------------

    def list_samba_accounts(self) -> list[SambaAccount]:
        # The unix account of an entry is gone after a recreation of the
        # container, so the entry carries the mark of the daemon in its
        # description field. A share user cannot change that field.
        output = self._run(["pdbedit", "-L", "-v"])
        entries: list[dict[str, str]] = []
        for line in output.splitlines():
            key, colon, value = line.partition(":")
            if not colon:
                continue
            if key == "Unix username":
                entries.append({})
            if entries:
                entries[-1][key] = value.strip()
        return [
            SambaAccount(
                username=entry["Unix username"],
                description=entry.get("Account desc", ""),
                enabled="D" not in entry.get("Account Flags", ""),
            )
            for entry in entries
        ]

    def list_unix_uids(self) -> dict[str, int]:
        return {entry.pw_name: entry.pw_uid for entry in pwd.getpwall()}

    def list_inbox_folders(self) -> list[InboxFolder]:
        with (
            _command_error(f"cannot list inbox folders in '{self._inbox}'"),
            os.scandir(self._inbox) as entries,
        ):
            folders = [
                InboxFolder(name=entry.name, uid=entry.stat().st_uid)
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
            ]
        return folders

    def read_usermap(self) -> tuple[str, ...] | None:
        # A map that a person saved in another encoding, or that a fault
        # damaged, would otherwise stop each cycle at the observation, so no
        # login would close and the map would never be written again. With
        # each such byte replaced, the lines differ from the wanted ones and
        # the cycle writes the map again.
        with _command_error(f"cannot read the username map '{self._usermap}'"):
            try:
                with open(self._usermap, encoding="utf-8", errors="replace") as fh:
                    return tuple(line.removesuffix("\n") for line in fh)
            except FileNotFoundError:
                return None

    # -- unix accounts -----------------------------------------------

    def create_unix_user(self, username: str, uid: int) -> None:
        existing = _unix_user(username)
        if existing is None:
            self._run(
                [
                    "useradd",
                    "--no-create-home",
                    "--home-dir",
                    "/nonexistent",
                    "--shell",
                    "/usr/sbin/nologin",
                    "--uid",
                    str(uid),
                    "--gid",
                    str(self._gid),
                    "--no-user-group",
                    username,
                ]
            )
            return
        if existing.pw_uid != uid:
            raise CommandError(
                f"unix user '{username}' exists with uid {existing.pw_uid}, "
                f"expected {uid}"
            )

    def delete_unix_user(self, username: str) -> None:
        if _unix_user(username) is not None:
            self._run(["userdel", username])

    # -- samba accounts ----------------------------------------------

    def set_samba_password(
        self, username: str, password: str, *, create: bool
    ) -> None:
        argv = ["smbpasswd", "-s"] + (["-a"] if create else []) + [username]
        self._run(argv, input_text=f"{password}\n{password}\n")

    def delete_samba_account(self, username: str) -> None:
        # idempotent so a retried deprovision after a partial failure succeeds
        if all(a.username != username for a in self.list_samba_accounts()):
            return
        # smbpasswd -x fails for an entry without a unix account
        self._run(["pdbedit", "-x", "-u", username])

    def disable_samba_account(self, username: str) -> None:
        self._run(["smbpasswd", "-d", username])

    def enable_samba_account(self, username: str) -> None:
        self._run(["smbpasswd", "-e", username])

    def store_mark(self, account: str, uid: int, joined: int) -> None:
        self._run(["pdbedit", "-r", "-u", account, "-N", entry_mark(uid, joined)])

    # -- username map ------------------------------------------------

    def write_usermap(self, lines: Sequence[str]) -> None:
        # smbd reads the map at each login. A login during a write into the
        # map itself sees a file without its last line, and without that line
        # an account name is a login name. So the lines go into a new file
        # that replaces the map in one step.
        fresh = self._usermap.with_name(f"{self._usermap.name}.new")
        with _command_error(f"cannot write the username map '{self._usermap}'"):
            fresh.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
            os.replace(fresh, self._usermap)

    # -- inbox folders -----------------------------------------------

    def create_inbox_folder(self, username: str, uid: int) -> None:
        path = self._inbox / username
        with _command_error(f"cannot create inbox folder '{path}'"):
            path.mkdir(exist_ok=True)
            os.chown(path, uid, self._gid)
            path.chmod(0o2770)

    def delete_inbox_folder(self, username: str) -> None:
        path = self._inbox / username
        if not path.exists():
            return
        with _command_error(f"cannot delete inbox folder '{path}'"):
            shutil.rmtree(path)
