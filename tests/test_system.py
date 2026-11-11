import pwd

import pytest

from smb_sync.model import InboxFolder, SambaAccount
from smb_sync.system import CommandError, SystemAdapter

PDBEDIT_OUTPUT = """\
alice:20005:0000000000000000000000000000000A:11112222333344445555666677778888:[U          ]:LCT-68C3A123:
bob:20006:0000000000000000000000000000000B:99990000AAAABBBBCCCCDDDDEEEEFFFF:[DU         ]:LCT-68C3A456:
"""


class FakeRun:
    def __init__(self, outputs=None, fail_prefix=None):
        self.calls = []
        self.outputs = outputs or {}
        self.fail_prefix = fail_prefix
        self.import_file_content = None

    def __call__(self, argv, input_text=None):
        self.calls.append((tuple(argv), input_text))
        if self.fail_prefix and tuple(argv[: len(self.fail_prefix)]) == self.fail_prefix:
            raise CommandError(f"{' '.join(argv)} failed")
        if argv[:2] == ["pdbedit", "-i"]:
            # capture the temp file before the adapter deletes it
            path = argv[2].removeprefix("smbpasswd:")
            with open(path, encoding="utf-8") as fh:
                self.import_file_content = fh.read()
        return self.outputs.get(tuple(argv), "")


def make_adapter(tmp_path, outputs=None, fail_prefix=None):
    run = FakeRun(outputs, fail_prefix=fail_prefix)
    return SystemAdapter(str(tmp_path), 10000, run=run), run


def no_such_user(monkeypatch):
    monkeypatch.setattr(
        pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name))
    )


def test_list_samba_accounts_parses_uid_and_flags(tmp_path):
    adapter, _ = make_adapter(
        tmp_path, {("pdbedit", "-L", "-w"): PDBEDIT_OUTPUT}
    )
    assert adapter.list_samba_accounts() == [
        SambaAccount("alice", 20005, True),
        SambaAccount("bob", 20006, False),
    ]


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
    adapter.create_unix_user("alice", 20005)
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
                "10000",
                "--no-user-group",
                "alice",
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


def test_rename_samba_account_exports_imports_then_deletes(tmp_path, monkeypatch):
    no_such_user(monkeypatch)
    line = "alice:20005:AAAA:BBBB:[U          ]:LCT-68C3A123:\n"
    adapter, run = make_adapter(tmp_path, {("pdbedit", "-L", "-w"): line})
    adapter.rename_samba_account("alice", "dave", 20005)
    assert run.import_file_content == "dave:20005:AAAA:BBBB:[U          ]:LCT-68C3A123:\n"
    # the unix rename must run between the export and the import: pdbedit
    # resolves the passdb uid via getpwnam, so it has to see the old name
    # for the export and the new name for the import
    assert [c[0][:2] for c in run.calls] == [
        ("pdbedit", "-L"),
        ("usermod", "--login"),
        ("pdbedit", "-i"),
        ("pdbedit", "-x"),
    ]
    assert run.calls[1][0] == ("usermod", "--login", "dave", "alice")
    assert run.calls[3][0] == ("pdbedit", "-x", "-u", "alice")


def test_rename_samba_account_skips_usermod_when_new_unix_exists(
    tmp_path, monkeypatch
):
    entry = pwd.struct_passwd(("dave", "x", 20005, 10000, "", "/", "/bin/false"))
    monkeypatch.setattr(pwd, "getpwnam", lambda name: entry)
    line = "alice:4294967295:AAAA:BBBB:[U          ]:LCT-68C3A123:\n"
    adapter, run = make_adapter(tmp_path, {("pdbedit", "-L", "-w"): line})
    adapter.rename_samba_account("alice", "dave", 20005)
    assert all(call[0][0] != "usermod" for call in run.calls)
    assert run.import_file_content == "dave:20005:AAAA:BBBB:[U          ]:LCT-68C3A123:\n"
    assert run.calls[-1][0] == ("pdbedit", "-x", "-u", "alice")


def test_rename_samba_account_noop_when_new_listed_and_old_gone(tmp_path):
    line = "dave:20005:AAAA:BBBB:[U          ]:LCT-68C3A123:\n"
    adapter, run = make_adapter(tmp_path, {("pdbedit", "-L", "-w"): line})
    adapter.rename_samba_account("alice", "dave", 20005)
    assert [c[0] for c in run.calls] == [("pdbedit", "-L", "-w")]


def test_rename_samba_account_deletes_old_when_both_listed(tmp_path):
    lines = (
        "alice:4294967295:AAAA:BBBB:[U          ]:LCT-68C3A123:\n"
        "dave:20005:AAAA:BBBB:[U          ]:LCT-68C3A123:\n"
    )
    adapter, run = make_adapter(tmp_path, {("pdbedit", "-L", "-w"): lines})
    adapter.rename_samba_account("alice", "dave", 20005)
    assert [c[0] for c in run.calls] == [
        ("pdbedit", "-L", "-w"),
        ("pdbedit", "-x", "-u", "alice"),
    ]


def test_rename_samba_account_import_failure_keeps_old_entry(
    tmp_path, monkeypatch
):
    no_such_user(monkeypatch)
    line = "alice:20005:AAAA:BBBB:[U          ]:LCT-68C3A123:\n"
    adapter, run = make_adapter(
        tmp_path,
        {("pdbedit", "-L", "-w"): line},
        fail_prefix=("pdbedit", "-i"),
    )
    with pytest.raises(CommandError):
        adapter.rename_samba_account("alice", "dave", 20005)
    # the old entry must never be deleted before the import succeeded
    assert all(call[0][:2] != ("pdbedit", "-x") for call in run.calls)


def test_rename_inbox_folder(tmp_path):
    (tmp_path / "alice").mkdir()
    adapter, _ = make_adapter(tmp_path)
    adapter.rename_inbox_folder("alice", "dave")
    assert not (tmp_path / "alice").exists()
    assert (tmp_path / "dave").is_dir()


def test_rename_inbox_folder_missing_old_is_noop(tmp_path):
    adapter, _ = make_adapter(tmp_path)
    adapter.rename_inbox_folder("alice", "dave")
    assert not (tmp_path / "dave").exists()


def test_rename_inbox_folder_conflict_raises(tmp_path):
    (tmp_path / "alice").mkdir()
    (tmp_path / "dave").mkdir()
    adapter, _ = make_adapter(tmp_path)
    with pytest.raises(CommandError, match="already exists"):
        adapter.rename_inbox_folder("alice", "dave")


def test_delete_inbox_folder_removes_contents(tmp_path):
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "scan.pdf").write_text("data")
    adapter, _ = make_adapter(tmp_path)
    adapter.delete_inbox_folder("alice")
    assert not (tmp_path / "alice").exists()


def test_delete_inbox_folder_missing_is_noop(tmp_path):
    adapter, _ = make_adapter(tmp_path)
    adapter.delete_inbox_folder("alice")


def test_list_inbox_folders_missing_root_raises(tmp_path):
    adapter, _ = make_adapter(tmp_path / "missing")
    with pytest.raises(CommandError):
        adapter.list_inbox_folders()


def test_rename_samba_account_missing_raises(tmp_path):
    adapter, _ = make_adapter(tmp_path)
    with pytest.raises(CommandError, match="not found"):
        adapter.rename_samba_account("ghost", "dave", 20005)


def test_delete_samba_account_missing_is_noop(tmp_path):
    adapter, run = make_adapter(tmp_path)
    adapter.delete_samba_account("ghost")
    assert all(call[0][0] != "smbpasswd" for call in run.calls)


def test_delete_samba_account_present_runs_smbpasswd(tmp_path):
    adapter, run = make_adapter(
        tmp_path, {("pdbedit", "-L", "-w"): PDBEDIT_OUTPUT}
    )
    adapter.delete_samba_account("alice")
    assert run.calls[-1] == (("smbpasswd", "-x", "alice"), None)
