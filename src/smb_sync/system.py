import os
import pwd
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from .model import InboxFolder, SambaAccount

# The entrypoint, the share template, the smoke test and the volume mounts of
# the two compose files use the same path, so they change together with this
# constant.
INBOX_ROOT = "/shares/inbox"

_STORED_UID = re.compile(r"smb-sync uid=([0-9]+)")


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


def _stored_uid(description: str) -> int | None:
    match = _STORED_UID.fullmatch(description)
    return int(match.group(1)) if match else None


def _run(argv: list[str], input_text: str | None = None) -> str:
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
    ):
        self._inbox = Path(inbox_path)
        self._gid = paperless_gid
        self._run = run or _run

    # -- observation -------------------------------------------------

    def list_samba_accounts(self) -> list[SambaAccount]:
        # pdbedit takes the uid of an entry from its unix account, which a
        # recreated container lacks, so the entry keeps its uid in the
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
                uid=_stored_uid(entry.get("Account desc", "")),
                enabled="D" not in entry.get("Account Flags", ""),
            )
            for entry in entries
        ]

    def list_unix_uids(self) -> dict[str, int]:
        return {entry.pw_name: entry.pw_uid for entry in pwd.getpwall()}

    def list_inbox_folders(self) -> list[InboxFolder]:
        folders = []
        with (
            _command_error(f"cannot list inbox folders in '{self._inbox}'"),
            os.scandir(self._inbox) as entries,
        ):
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    folders.append(
                        InboxFolder(name=entry.name, uid=entry.stat().st_uid)
                    )
        return folders

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
        # smbpasswd -x fails for an entry without a unix account, such as the
        # old name of a rename that stopped before its last step
        self._run(["pdbedit", "-x", "-u", username])

    def disable_samba_account(self, username: str) -> None:
        self._run(["smbpasswd", "-d", username])

    def enable_samba_account(self, username: str) -> None:
        self._run(["smbpasswd", "-e", username])

    def store_uid(self, username: str, uid: int) -> None:
        self._run(["pdbedit", "-r", "-u", username, "-N", f"smb-sync uid={uid}"])

    def rename_samba_account(self, old: str, new: str, uid: int) -> None:
        # tdbsam cannot rename in place, so the hash line is re-imported
        # under the new name. Each step either checks whether an earlier call
        # did it or can safely run again, so the next cycle finishes a rename
        # that stopped anywhere.
        # The unix account comes first because pdbedit imports a line only
        # for a name that getpwnam resolves. It moves when the old name still
        # holds it, since useradd refuses a second account with its uid.
        # The old entry goes last: until the new entry stores the uid, the old
        # one is how the planner finds the open rename, and without it the
        # next cycle creates the account again with a new password.
        # The hash line comes from the full listing because one call shows
        # both names.
        output = self._run(["pdbedit", "-L", "-w"])
        lines = {
            line.split(":", 1)[0]: line
            for line in output.splitlines()
            if ":" in line
        }
        if old not in lines and new not in lines:
            raise CommandError(f"samba account '{old}' not found")
        if _unix_user(new) is None:
            if _unix_user(old) is not None:
                self._run(["usermod", "--login", new, old])
            else:
                self.create_unix_user(new, uid)
        if new not in lines:
            parts = lines[old].split(":")
            parts[0] = new
            parts[1] = str(uid)  # exported uid is unreliable once getpwnam(old) fails
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".smbpasswd"
            ) as fh:
                fh.write(":".join(parts) + "\n")
                # pdbedit opens the file by name while this handle is still open
                fh.flush()
                self._run(["pdbedit", "-i", f"smbpasswd:{fh.name}"])
        # The smbpasswd format has no description field, so the import loses
        # the uid. store_uid fails when pdbedit -i refused the line with exit 0,
        # so the old entry stays.
        self.store_uid(new, uid)
        if old in lines:
            self._run(["pdbedit", "-x", "-u", old])

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

    def rename_inbox_folder(self, old: str, new: str) -> None:
        old_path = self._inbox / old
        new_path = self._inbox / new
        if not old_path.exists():
            return
        if new_path.exists():
            raise CommandError(
                f"cannot rename inbox folder '{old}': '{new}' already exists"
            )
        with _command_error(
            f"cannot rename inbox folder '{old_path}' to '{new_path}'"
        ):
            old_path.rename(new_path)
