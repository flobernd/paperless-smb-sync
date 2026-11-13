import json
import os
import pwd
import signal
import stat
import subprocess

import pytest

from smb_sync.model import InboxFolder, SambaAccount
from smb_sync.system import CommandError, SystemAdapter

PDBEDIT_VERBOSE = """\
---------------
Unix username:        u20005
Account Flags:        [U          ]
User SID:             S-1-5-21-1-2-3-1000
Account desc:         smb-sync uid=20005 joined=1731405600123
Logon time:           Wed, 11 Nov 2026 22:00:00 UTC
---------------
Unix username:        u20006
Account Flags:        [DU         ]
Account desc:
---------------
Unix username:        desk
Account Flags:        [U          ]
Account desc:         Front desk scanner: do not delete
"""


class FakeRun:
    def __init__(self, outputs=None, fail_prefix=None):
        self.calls = []
        self.outputs = outputs or {}
        self.fail_prefix = fail_prefix

    def __call__(self, argv, input_text=None):
        self.calls.append((tuple(argv), input_text))
        if self.fail_prefix and tuple(argv[: len(self.fail_prefix)]) == self.fail_prefix:
            raise CommandError(f"{' '.join(argv)} failed")
        return self.outputs.get(tuple(argv), "")


def make_adapter(tmp_path, outputs=None, fail_prefix=None):
    run = FakeRun(outputs, fail_prefix=fail_prefix)
    return SystemAdapter(str(tmp_path), 10000, run=run), run


def test_listing_reads_the_description_and_the_flag_of_each_entry(tmp_path):
    adapter, _ = make_adapter(tmp_path, {("pdbedit", "-L", "-v"): PDBEDIT_VERBOSE})
    assert adapter.list_samba_accounts() == [
        SambaAccount("u20005", "smb-sync uid=20005 joined=1731405600123", True),
        SambaAccount("u20006", "", False),
        SambaAccount("desk", "Front desk scanner: do not delete", True),
    ]


def test_a_command_that_cannot_start_raises_a_command_error(tmp_path, monkeypatch):
    # no directory of this PATH holds pdbedit, and the adapter keeps its own runner
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = SystemAdapter(str(tmp_path), 10000)
    with pytest.raises(CommandError, match="pdbedit"):
        adapter.list_samba_accounts()


def test_list_inbox_folders(tmp_path):
    (tmp_path / "alice").mkdir()
    (tmp_path / "somefile.txt").write_text("not a folder")
    adapter, _ = make_adapter(tmp_path)
    folders = adapter.list_inbox_folders()
    assert len(folders) == 1
    assert folders[0].name == "alice"
    assert isinstance(folders[0], InboxFolder)


def test_create_unix_user_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name))
    )
    adapter, run = make_adapter(tmp_path)
    adapter.create_unix_user("u20005", 20005)
    assert run.calls == [
        (
            (
                "useradd",
                "--no-create-home",
                "--home-dir",
                "/nonexistent",
                "--shell",
                "/usr/sbin/nologin",
                "--uid",
                "20005",
                "--gid",
                "smb-sync",
                "--no-user-group",
                "u20005",
            ),
            None,
        )
    ]


def test_create_unix_user_skips_existing_matching_uid(tmp_path, monkeypatch):
    entry = pwd.struct_passwd(("alice", "x", 20005, 10000, "", "/", "/bin/false"))
    monkeypatch.setattr(pwd, "getpwnam", lambda name: entry)
    adapter, run = make_adapter(tmp_path)
    adapter.create_unix_user("alice", 20005)
    assert run.calls == []


def test_create_unix_user_uid_conflict_raises(tmp_path, monkeypatch):
    entry = pwd.struct_passwd(("alice", "x", 1000, 1000, "", "/", "/bin/false"))
    monkeypatch.setattr(pwd, "getpwnam", lambda name: entry)
    adapter, _ = make_adapter(tmp_path)
    with pytest.raises(CommandError, match="uid 1000"):
        adapter.create_unix_user("alice", 20005)


def test_delete_unix_user_missing_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name))
    )
    adapter, run = make_adapter(tmp_path)
    adapter.delete_unix_user("alice")
    assert run.calls == []


def test_set_samba_password_create_and_update(tmp_path):
    adapter, run = make_adapter(tmp_path)
    adapter.set_samba_password("alice", "pw1", create=True)
    adapter.set_samba_password("alice", "pw2", create=False)
    assert run.calls == [
        (("smbpasswd", "-s", "-a", "alice"), "pw1\npw1\n"),
        (("smbpasswd", "-s", "alice"), "pw2\npw2\n"),
    ]


def test_disable_and_enable_send_their_own_flag(tmp_path):
    adapter, run = make_adapter(tmp_path)
    adapter.disable_samba_account("alice")
    adapter.enable_samba_account("alice")
    assert [call[0] for call in run.calls] == [
        ("smbpasswd", "-d", "alice"),
        ("smbpasswd", "-e", "alice"),
    ]


def test_a_folder_creation_follows_no_link_under_the_account_name(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    (inbox / "u20005").symlink_to(outside)
    # the ids of the test user, so a creation that follows the link changes the target without root
    adapter = SystemAdapter(str(inbox), os.getgid())
    with pytest.raises(CommandError, match="cannot create inbox folder"):
        adapter.create_inbox_folder("u20005", os.getuid())
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


def test_delete_inbox_folder_removes_contents(tmp_path):
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "scan.pdf").write_text("data")
    adapter, _ = make_adapter(tmp_path)
    adapter.delete_inbox_folder("alice")
    assert not (tmp_path / "alice").exists()


def test_delete_inbox_folder_missing_is_noop(tmp_path):
    adapter, _ = make_adapter(tmp_path)
    adapter.delete_inbox_folder("alice")


def test_a_folder_repair_sets_the_group_below_and_follows_no_link(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    folder = root / "inbox" / "u20005"
    (folder / "2026").mkdir(parents=True)
    (folder / "2026" / "scan.pdf").write_text("scan")
    (root / "outside").mkdir()
    (root / "outside" / "letter.pdf").write_text("letter")
    (folder / "link").symlink_to(root / "outside")
    changed = []

    def chown(path, uid, gid, *, dir_fd=None, follow_symlinks=True):
        # the object that the call changes: the target of a link when the call follows it
        if dir_fd is not None:
            # Linux names the folder of a descriptor under /proc
            path = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), path)
        target = os.path.realpath(path) if follow_symlinks else os.fspath(path)
        changed.append((os.path.relpath(target, root), uid, gid))

    monkeypatch.setattr(os, "chown", chown)
    SystemAdapter(str(root / "inbox"), 10000).repair_inbox_folder("u20005", 20005)
    assert sorted(changed) == [
        ("inbox/u20005", 20005, 10000),
        ("inbox/u20005/2026", -1, 10000),
        ("inbox/u20005/2026/scan.pdf", -1, 10000),
        ("inbox/u20005/link", -1, 10000),
    ]


def test_a_folder_repair_that_cannot_read_a_subfolder_leaves_the_group_of_the_folder(tmp_path, monkeypatch):
    folder = tmp_path / "u20005"
    (folder / "2026").mkdir(parents=True)
    changed = []

    def chown(path, uid, gid, *, dir_fd=None, follow_symlinks=True):
        changed.append(os.path.basename(path))
        if os.path.basename(path) == "2026":
            # the walk finds no subfolder to read when it gets there
            (folder / "2026").rmdir()

    monkeypatch.setattr(os, "chown", chown)
    with pytest.raises(CommandError):
        SystemAdapter(str(tmp_path), 10000).repair_inbox_folder("u20005", 20005)
    assert changed == ["2026"]


def test_a_folder_repair_takes_the_setgid_bit_off_the_folder_while_it_walks(tmp_path, monkeypatch):
    folder = tmp_path / "u20005"
    folder.mkdir()
    folder.chmod(0o2770)
    # an environment that cannot hold the bit fails the test here, with that cause
    assert folder.stat().st_mode & stat.S_ISGID
    (folder / "scan.pdf").write_text("scan")
    during = []

    def chown(path, uid, gid, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None:
            # a change below the folder, so the walk is not at its end
            during.append(stat.S_IMODE(folder.stat().st_mode))

    monkeypatch.setattr(os, "chown", chown)
    SystemAdapter(str(tmp_path), 10000).repair_inbox_folder("u20005", 20005)
    assert during == [0o770]
    assert stat.S_IMODE(folder.stat().st_mode) == 0o2770


def test_list_inbox_folders_missing_root_raises(tmp_path):
    adapter, _ = make_adapter(tmp_path / "missing")
    with pytest.raises(CommandError):
        adapter.list_inbox_folders()


def test_delete_samba_account_missing_is_noop(tmp_path):
    adapter, _ = make_adapter(
        tmp_path,
        {("pdbedit", "-L", "-v"): PDBEDIT_VERBOSE},
        fail_prefix=("pdbedit", "-x", "-u", "ghost"),
    )
    adapter.delete_samba_account("ghost")


def test_a_failed_delete_of_a_listed_entry_raises(tmp_path):
    adapter, _ = make_adapter(
        tmp_path,
        {("pdbedit", "-L", "-v"): PDBEDIT_VERBOSE},
        fail_prefix=("pdbedit", "-x", "-u", "u20005"),
    )
    with pytest.raises(CommandError, match="pdbedit -x -u u20005"):
        adapter.delete_samba_account("u20005")


def test_a_map_write_moves_a_new_file_over_the_old_one(tmp_path):
    adapter = SystemAdapter(str(tmp_path), 10000, state_dir=str(tmp_path))
    assert adapter.read_usermap() is None
    adapter.write_usermap(("!nobody = *",))
    first = (tmp_path / "smb-sync-usermap").stat().st_ino
    adapter.write_usermap(("!u20005 = alice", "!nobody = *"))
    assert (tmp_path / "smb-sync-usermap").stat().st_ino != first
    assert adapter.read_usermap() == ("!u20005 = alice", "!nobody = *")
    assert [path.name for path in tmp_path.iterdir()] == ["smb-sync-usermap"]


def test_a_map_that_is_no_utf_8_reads_as_lines_that_differ(tmp_path):
    # the byte of an "ö" from an editor that saved the file as Latin-1
    (tmp_path / "smb-sync-usermap").write_bytes(b"!u20005 = J\xf6rg\n!nobody = *\n")
    adapter = SystemAdapter(str(tmp_path), 10000, state_dir=str(tmp_path))
    assert adapter.read_usermap() == ("!u20005 = J\ufffdrg", "!nobody = *")


def session_list(*sessions):
    # the fields that the adapter reads from `smbstatus -b --json`, with the process id as text, as Samba prints it
    return json.dumps(
        {
            "sessions": {
                str(number): {"username": account, "server_id": {"pid": str(pid)}}
                for number, (account, pid) in enumerate(sessions)
            }
        }
    )


def test_a_session_stop_ends_the_sessions_of_that_account_only(tmp_path):
    mine = subprocess.Popen(["sleep", "60"])
    other = subprocess.Popen(["sleep", "60"])
    try:
        status = session_list(("u20005", mine.pid), ("u20006", other.pid))
        adapter, _ = make_adapter(tmp_path, {("smbstatus", "-b", "--json"): status})
        adapter.stop_sessions("u20005")
        assert mine.wait(timeout=5) == -signal.SIGTERM
        # a signal that reached the other session ends it within this time
        with pytest.raises(subprocess.TimeoutExpired):
            other.wait(timeout=0.5)
    finally:
        for process in (mine, other):
            process.kill()
            process.wait()


def test_a_session_that_ended_in_the_meantime_does_not_keep_the_next_one_open(tmp_path):
    ended = subprocess.Popen(["true"])
    ended.wait()
    live = subprocess.Popen(["sleep", "60"])
    try:
        status = session_list(("u20005", ended.pid), ("u20005", live.pid))
        adapter, _ = make_adapter(tmp_path, {("smbstatus", "-b", "--json"): status})
        adapter.stop_sessions("u20005")
        assert live.wait(timeout=5) == -signal.SIGTERM
    finally:
        live.kill()
        live.wait()


def test_a_session_list_with_the_process_id_1_ends_no_process(tmp_path, monkeypatch):
    signaled = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signaled.append(pid))
    status = session_list(("u20005", 4242), ("u20005", 1))
    adapter, _ = make_adapter(tmp_path, {("smbstatus", "-b", "--json"): status})
    with pytest.raises(CommandError, match="smbstatus"):
        adapter.stop_sessions("u20005")
    assert signaled == []
