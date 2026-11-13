import json
import os
import pwd
import shutil
import signal
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
# container together with the entries that its lines name. The `username map`
# line of the share template and the smoke test use the path of the map, so
# they change together with this constant and _USERMAP.
STATE_DIR = "/var/lib/samba"

# The entrypoint makes this group and the share template admits its members,
# so the three change together.
ACCOUNT_GROUP = "smb-sync"

UID_BASE_FILE = "smb-sync-uid-base"
_FOLDER_MODE = 0o2770
_USERMAP = "smb-sync-usermap"


class CommandError(Exception):
    pass


@contextmanager
def _command_error(what: str) -> Generator[None, None, None]:
    try:
        yield
    except OSError as exc:
        raise CommandError(f"{what}: {exc}") from exc


def _raise_error(error: OSError) -> None:
    raise error


def _replace_file(path: Path, text: str) -> None:
    # After a power loss, a file that was moved before its content reached the
    # disk can be empty. Under an empty map each login name fails and each
    # account name is a login name, and an empty base file refuses each later
    # start.
    fresh = path.with_name(f"{path.name}.new")
    with open(fresh, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(fresh, path)


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


def _process_id(text: str) -> int:
    pid = int(text)
    # A signal to 0 reaches the process group of the daemon, one to -1 each
    # process of the container, and 1 is the init of the container.
    if pid < 2:
        raise ValueError(f"process id {pid} is below 2")
    return pid


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
        self._uid_base = Path(state_dir) / UID_BASE_FILE
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

    def list_unix_names(self) -> frozenset[str]:
        return frozenset(entry.pw_name for entry in pwd.getpwall())

    def list_inbox_folders(self) -> list[InboxFolder]:
        folders: list[InboxFolder] = []
        with (
            _command_error(f"cannot list inbox folders in '{self._inbox}'"),
            os.scandir(self._inbox) as entries,
        ):
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    stat = entry.stat()
                    folders.append(
                        InboxFolder(name=entry.name, uid=stat.st_uid, gid=stat.st_gid)
                    )
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
            # The accounts are no members of the group of paperless, so a share
            # that does not force the group cannot open the folder of another
            # account. The managed share forces it, so each file of a session
            # still gets the group that paperless reads.
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
                    ACCOUNT_GROUP,
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
        # smbpasswd -x fails for an entry without a unix account
        try:
            self._run(["pdbedit", "-x", "-u", username])
        except CommandError:
            # idempotent so a retried deprovision after a partial failure succeeds
            if any(a.username == username for a in self.list_samba_accounts()):
                raise

    def disable_samba_account(self, username: str) -> None:
        self._run(["smbpasswd", "-d", username])

    def enable_samba_account(self, username: str) -> None:
        self._run(["smbpasswd", "-e", username])

    def stop_sessions(self, account: str) -> None:
        # A disabled entry keeps each session that started before. A
        # close-share of smbcontrol leaves the session open too, so the stop
        # ends the smbd process of each one. That process serves one client
        # connection, so a session of another account on that connection
        # ends with it.
        output = self._run(["smbstatus", "-b", "--json"])
        try:
            pids = [
                _process_id(session["server_id"]["pid"])
                for session in json.loads(output)["sessions"].values()
                if session["username"] == account
            ]
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise CommandError(
                f"cannot read the session list of smbstatus: {exc!r}"
            ) from exc
        for pid in pids:
            with _command_error(
                f"cannot end process {pid} of a session of '{account}'"
            ):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    # the session ended after the listing
                    pass

    def store_mark(self, account: str, uid: int, joined: int) -> None:
        self._run(["pdbedit", "-r", "-u", account, "-N", entry_mark(uid, joined)])

    # -- username map ------------------------------------------------

    def write_usermap(self, lines: Sequence[str]) -> None:
        # smbd reads the map at each login. A login during a write into the
        # map itself sees a file without its last line, and without that line
        # an account name is a login name. So the lines go into a new file
        # that replaces the map in one step.
        with _command_error(f"cannot write the username map '{self._usermap}'"):
            _replace_file(self._usermap, "".join(f"{line}\n" for line in lines))

    # -- stored UID_BASE ---------------------------------------------

    def read_uid_base(self) -> str | None:
        with _command_error(f"cannot read the stored UID_BASE '{self._uid_base}'"):
            try:
                return self._uid_base.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                return None

    def write_uid_base(self, uid_base: int) -> None:
        with _command_error(f"cannot write the stored UID_BASE '{self._uid_base}'"):
            _replace_file(self._uid_base, f"{uid_base}\n")

    # -- inbox folders -----------------------------------------------

    def create_inbox_folder(self, username: str, uid: int) -> None:
        path = self._inbox / username
        with _command_error(f"cannot create inbox folder '{path}'"):
            path.mkdir(exist_ok=True)
            os.chown(path, uid, self._gid)
            path.chmod(_FOLDER_MODE)

    def repair_inbox_folder(self, account: str, uid: int) -> None:
        path = self._inbox / account
        with _command_error(f"cannot repair inbox folder '{path}'"):
            # With the setgid bit and the old group on the folder, the kernel
            # gives the old group to each entry that a session makes during the
            # walk, and the walk does not list it. Without the bit such an entry
            # gets the group of the session, which the share forces to the
            # group of paperless.
            os.chmod(path, 0o770)
            # A share user can swap a subfolder for a link between the listing
            # and the change, and the daemon runs as root. fwalk opens each
            # subfolder through the descriptor of its parent and enters no
            # link, and each change names its file through the descriptor of
            # the folder that listed it and follows no link. So no change
            # reaches a file outside the inbox.
            # The planner compares the folder alone, so its owner and its group
            # go last, and a repair that stopped below it is planned again in
            # the next cycle. For the same reason a subfolder that the walk
            # cannot read ends the repair, where fwalk would pass over it by
            # default.
            for _, dirnames, filenames, dir_fd in os.fwalk(path, onerror=_raise_error):
                for name in (*dirnames, *filenames):
                    os.chown(name, -1, self._gid, dir_fd=dir_fd, follow_symlinks=False)
            os.chown(path, uid, self._gid)
            path.chmod(_FOLDER_MODE)

    def delete_inbox_folder(self, username: str) -> None:
        path = self._inbox / username
        if not path.exists():
            return
        with _command_error(f"cannot delete inbox folder '{path}'"):
            shutil.rmtree(path)
