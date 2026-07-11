import os
import pwd
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from .model import InboxFolder, SambaAccount


class CommandError(Exception):
    pass


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
        output = self._run(["pdbedit", "-L", "-w"])
        accounts = []
        for line in output.splitlines():
            parts = line.split(":")
            if len(parts) < 5:
                continue
            try:
                uid = int(parts[1])
            except ValueError:
                continue
            accounts.append(
                SambaAccount(username=parts[0], uid=uid, enabled="D" not in parts[4])
            )
        return accounts

    def list_inbox_folders(self) -> list[InboxFolder]:
        folders = []
        try:
            with os.scandir(self._inbox) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        folders.append(
                            InboxFolder(name=entry.name, uid=entry.stat().st_uid)
                        )
        except OSError as exc:
            raise CommandError(
                f"cannot list inbox folders in '{self._inbox}': {exc}"
            ) from exc
        return folders

    # -- unix accounts -----------------------------------------------

    def create_unix_user(self, username: str, uid: int) -> None:
        try:
            existing = pwd.getpwnam(username)
        except KeyError:
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
        try:
            pwd.getpwnam(username)
        except KeyError:
            return
        self._run(["userdel", username])

    # -- samba accounts ----------------------------------------------

    def set_samba_password(
        self, username: str, password: str, create: bool = False
    ) -> None:
        argv = ["smbpasswd", "-s"] + (["-a"] if create else []) + [username]
        self._run(argv, input_text=f"{password}\n{password}\n")

    def delete_samba_account(self, username: str) -> None:
        # idempotent so a retried deprovision after a partial failure succeeds
        if all(a.username != username for a in self.list_samba_accounts()):
            return
        self._run(["smbpasswd", "-x", username])

    def disable_samba_account(self, username: str) -> None:
        self._run(["smbpasswd", "-d", username])

    def enable_samba_account(self, username: str) -> None:
        self._run(["smbpasswd", "-e", username])

    def rename_samba_account(self, old: str, new: str) -> None:
        # tdbsam resolves the passdb uid via getpwnam(username), so the
        # export must happen before the unix rename below: once "old" no
        # longer resolves, pdbedit returns a placeholder uid (4294967295)
        # and the reimport silently fails. The old entry is deleted last
        # so a crash never loses the account.
        output = self._run(["pdbedit", "-L", "-w", "-u", old])
        lines = output.strip().splitlines()
        if not lines:
            raise CommandError(f"samba account '{old}' not found")
        parts = lines[0].split(":")
        parts[0] = new
        self._run(["usermod", "--login", new, old])
        fd, temp_path = tempfile.mkstemp(suffix=".smbpasswd")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(":".join(parts) + "\n")
            self._run(["pdbedit", "-i", f"smbpasswd:{temp_path}"])
        finally:
            os.unlink(temp_path)
        self._run(["pdbedit", "-x", "-u", old])

    # -- inbox folders -----------------------------------------------

    def create_inbox_folder(self, username: str, uid: int) -> None:
        path = self._inbox / username
        try:
            path.mkdir(exist_ok=True)
            os.chown(path, uid, self._gid)
            path.chmod(0o2770)
        except OSError as exc:
            raise CommandError(
                f"cannot create inbox folder '{path}': {exc}"
            ) from exc

    def delete_inbox_folder(self, username: str) -> None:
        path = self._inbox / username
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise CommandError(
                f"cannot delete inbox folder '{path}': {exc}"
            ) from exc

    def rename_inbox_folder(self, old: str, new: str) -> None:
        old_path = self._inbox / old
        new_path = self._inbox / new
        if not old_path.exists():
            return
        if new_path.exists():
            raise CommandError(
                f"cannot rename inbox folder '{old}': '{new}' already exists"
            )
        try:
            old_path.rename(new_path)
        except OSError as exc:
            raise CommandError(
                f"cannot rename inbox folder '{old_path}' to '{new_path}': {exc}"
            ) from exc
