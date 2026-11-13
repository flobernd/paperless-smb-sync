from dataclasses import replace

import pytest

from smb_sync.model import (
    ActualState,
    CreateAccount,
    CreateFolder,
    CreateWorkflow,
    CredentialDocument,
    DeleteDocument,
    DeleteWorkflow,
    Deprovision,
    DisableAccount,
    EnableAccount,
    HandOverDocument,
    InboxFolder,
    ManagedWorkflow,
    PaperlessUser,
    RenameDocument,
    RepairUnixAccount,
    SambaAccount,
    TokenUser,
    UpdateWorkflow,
    WriteMap,
)
from smb_sync.planner import plan_cycle

UID_BASE = 20000
JOINED = 1731405600123
ADMIN = TokenUser(1, True)


def planned(users, actual):
    return plan_cycle(users, actual, UID_BASE)


def member(user_id, username, active=True, joined=JOINED):
    return PaperlessUser(user_id, username, active, joined, True)


def former(user_id, username):
    return PaperlessUser(user_id, username, True, JOINED, False)


def entry(user_id, enabled=True, joined=JOINED):
    uid = UID_BASE + user_id
    return SambaAccount(f"u{uid}", f"smb-sync uid={uid} joined={joined}", enabled)


def folder(user_id):
    return InboxFolder(f"u{UID_BASE + user_id}", UID_BASE + user_id)


def workflow(workflow_id, user_id):
    return ManagedWorkflow(workflow_id, f"u{UID_BASE + user_id}", user_id)


def state(**fields):
    empty = ActualState(
        accounts=(),
        folders=(),
        workflows=(),
        unix_uids={},
        map_lines=("!nobody = *",),
        documents=(),
        token_user=None,
    )
    return replace(empty, **fields)


def settled(*logins):
    # one complete account for each pair of a user id and a login name, with the workflow id 100 + user id
    ids = [user_id for user_id, _ in logins]
    return state(
        accounts=tuple(entry(i) for i in ids),
        folders=tuple(folder(i) for i in ids),
        workflows=tuple(workflow(100 + i, i) for i in ids),
        unix_uids={f"u{UID_BASE + i}": UID_BASE + i for i in ids},
        map_lines=tuple(f"!u{UID_BASE + i} = {name}" for i, name in logins) + ("!nobody = *",),
    )


def test_a_settled_state_plans_no_action():
    # paperless lists the users in the order of the usernames, and the map is in the order of the uids
    users = [member(9, "anna"), member(5, "zoe")]
    plan = planned(users, settled((5, "zoe"), (9, "anna")))
    assert plan.actions == ()
    assert plan.warnings == ()


def test_a_rename_in_paperless_writes_the_map_and_retitles_the_document():
    actual = replace(
        settled((5, "alice")),
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 5, recipient_id=None),),
    )
    assert planned([member(5, "dave")], actual).actions == (
        WriteMap(("!u20005 = dave", "!nobody = *")),
        RenameDocument(document_id=40, old_username="alice", new_username="dave"),
    )


def test_a_member_whose_entry_has_no_mark_gets_the_account_made_again():
    # a creation that stopped between the password and the mark
    alice = member(5, "alice")
    actual = state(accounts=(SambaAccount("u20005", "", True),), unix_uids={"u20005": 20005})
    plan = planned([alice], actual)
    assert plan.actions == (
        WriteMap(("!u20005 = alice", "!nobody = *")),
        CreateAccount(user=alice, uid=20005, upload=False),
        CreateWorkflow(user_id=5, account="u20005"),
    )
    assert plan.warnings == ()


def test_an_inactive_member_gets_a_closed_login():
    plan = planned([member(5, "alice", active=False)], settled((5, "alice")))
    assert plan.actions == (DisableAccount(account="u20005"),)


def test_a_reactivated_member_gets_an_open_login():
    actual = replace(settled((5, "alice")), accounts=(entry(5, enabled=False),))
    assert planned([member(5, "alice")], actual).actions == (EnableAccount(account="u20005"),)


def test_a_new_inactive_member_gets_a_closed_login():
    bob = member(6, "bob", active=False)
    assert planned([bob], state()).actions == (
        WriteMap(("!u20006 = bob", "!nobody = *")),
        CreateAccount(user=bob, uid=20006, upload=False),
        CreateWorkflow(user_id=6, account="u20006"),
        DisableAccount(account="u20006"),
    )


def test_a_missing_folder_and_a_missing_workflow_are_made_again():
    actual = replace(settled((5, "alice")), folders=(), workflows=())
    assert planned([member(5, "alice")], actual).actions == (
        CreateFolder(account="u20005", uid=20005),
        CreateWorkflow(user_id=5, account="u20005"),
    )


@pytest.mark.parametrize("defined_for", [9, None])
def test_a_workflow_that_is_no_definition_for_its_member_is_written_back(defined_for):
    # a person gave the workflow to user 9, or changed one field of it
    changed = ManagedWorkflow(105, "u20005", defined_for)
    actual = replace(settled((5, "alice")), workflows=(changed,))
    assert planned([member(5, "alice")], actual).actions == (
        UpdateWorkflow(workflow_id=105, user_id=5, account="u20005"),
    )


def test_a_folder_with_another_owner_is_taken_back():
    actual = replace(settled((5, "alice")), folders=(InboxFolder("u20005", 1000),))
    assert planned([member(5, "alice")], actual).actions == (
        CreateFolder(account="u20005", uid=20005),
    )


def test_a_missing_unix_account_is_made_again_from_the_mark():
    actual = replace(settled((5, "alice")), unix_uids={})
    assert planned([member(5, "alice")], actual).actions == (
        RepairUnixAccount(account="u20005", uid=20005),
    )


def test_a_removal_keeps_a_folder_of_another_owner():
    # user 6 left the group, and the folder under the account name has another owner than the entry
    users = [member(5, "alice"), former(6, "bob")]
    actual = replace(
        settled((5, "alice"), (6, "bob")),
        folders=(folder(5), InboxFolder("u20006", 20009)),
    )
    assert planned(users, actual).actions == (
        WriteMap(("!u20005 = alice", "!nobody = *")),
        Deprovision(
            account="u20006",
            user_id=6,
            login_name="bob",
            workflow_id=106,
            delete_folder=False,
            documents=None,
        ),
    )


def test_each_removal_candidate_goes_in_the_cycle_that_finds_it():
    # user 6 left the group, and paperless has no user 7 any more
    users = [member(5, "alice"), former(6, "bob")]
    actual = settled((5, "alice"), (6, "bob"), (7, "carol"))
    assert planned(users, actual).actions == (
        WriteMap(("!u20005 = alice", "!nobody = *")),
        Deprovision(
            account="u20006",
            user_id=6,
            login_name="bob",
            workflow_id=106,
            delete_folder=True,
            documents=None,
        ),
        Deprovision(
            account="u20007",
            user_id=7,
            login_name="carol",
            workflow_id=107,
            delete_folder=True,
            documents=None,
        ),
    )


def test_a_workflow_of_no_account_is_deleted():
    # version 0.1.0 named its workflows after the usernames
    actual = replace(
        settled((5, "alice")),
        workflows=(workflow(105, 5), ManagedWorkflow(8, "alice", 5)),
    )
    assert planned([member(5, "alice")], actual).actions == (DeleteWorkflow(workflow_id=8),)


def test_a_folder_of_no_account_is_left_with_a_warning():
    plan = planned([], state(folders=(InboxFolder("archive", 65534),)))
    assert plan.actions == ()
    assert any("archive" in w for w in plan.warnings)


@pytest.mark.parametrize(
    "other",
    [
        # below UID_BASE
        SambaAccount("u19999", "smb-sync uid=19999 joined=1731405600123", True),
        # the mark of version 0.1.0
        SambaAccount("u20006", "smb-sync uid=20006", True),
        SambaAccount("u20006", "smb-sync uid=20006 joined=1731405600123 old", True),
        # not the account name of its uid
        SambaAccount("bob", "smb-sync uid=20006 joined=1731405600123", True),
        SambaAccount("desk", "Front desk scanner: do not delete", True),
    ],
)
def test_an_entry_that_does_not_count_is_left_with_a_warning(other):
    actual = replace(settled((5, "alice")), accounts=(entry(5), other))
    plan = planned([member(5, "alice")], actual)
    assert plan.actions == ()
    assert any(other.username in w for w in plan.warnings)


def test_the_first_uid_of_the_range_counts():
    # the user id 0 gives the uid UID_BASE itself, and paperless has no such user
    actual = replace(settled((5, "alice")), accounts=(entry(0), entry(5)))
    assert planned([member(5, "alice")], actual).actions == (
        Deprovision(
            account="u20000",
            user_id=0,
            login_name=None,
            workflow_id=None,
            delete_folder=False,
            documents=None,
        ),
    )


def test_a_member_whose_name_cannot_be_a_login_keeps_the_account_and_gets_no_document_rename():
    actual = replace(
        settled((5, "alice")),
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 5, recipient_id=None),),
    )
    plan = planned([member(5, "Bad Name")], actual)
    assert plan.actions == (WriteMap(("!nobody = *",)),)
    assert any("Bad Name" in w for w in plan.warnings)


@pytest.mark.parametrize("username", ["John.Doe", "anne-marie@example.com", "anna+scan", "Jörg"])
def test_a_name_inside_the_set_of_paperless_gets_a_line(username):
    assert planned([member(5, username)], state()).logins == {"u20005": username}


@pytest.mark.parametrize("username", ["@alice", "+alice", "anna smith"])
def test_a_member_whose_name_cannot_be_in_the_map_gets_no_account(username):
    plan = planned([member(5, username)], state())
    assert plan.actions == ()
    assert any(username in w for w in plan.warnings)


def test_of_two_new_members_equal_without_case_the_lowest_id_keeps_the_name():
    first = member(5, "Alice")
    plan = planned([member(6, "alice"), first], state())
    assert plan.actions == (
        WriteMap(("!u20005 = Alice", "!nobody = *")),
        CreateAccount(user=first, uid=20005, upload=False),
        CreateWorkflow(user_id=5, account="u20005"),
    )
    assert any("'alice'" in w for w in plan.warnings)


def test_the_member_whose_account_has_the_name_in_the_map_keeps_it():
    # user 6 has the login since the last write of the map, in another case than the username has now
    users = [member(5, "ALICE"), member(6, "Alice")]
    plan = planned(users, settled((6, "alice")))
    assert plan.actions == (WriteMap(("!u20006 = Alice", "!nobody = *")),)
    assert any("'ALICE'" in w for w in plan.warnings)


@pytest.mark.parametrize(("first", "second"), [("straße", "strasse"), ("ßs", "sß")])
def test_two_names_that_samba_tells_apart_both_get_a_line(first, second):
    # casefold() gives one text for both names of a pair, and Samba compares character by character
    users = [member(5, first), member(6, second)]
    assert planned(users, state()).logins == {"u20005": first, "u20006": second}


def test_two_names_that_samba_takes_for_one_get_one_line():
    # Samba takes the final sigma and the sigma for one letter, and str.lower() keeps them apart
    users = [member(5, "σοφος"), member(6, "σοφοσ")]
    assert planned(users, state()).logins == {"u20005": "σοφος"}


def test_a_waiting_document_goes_to_the_user_with_its_id_and_keeps_its_name():
    # uploaded for user 5 under the name alice, who is dave now, while user 9 took the name alice
    users = [member(5, "dave"), member(9, "alice")]
    actual = replace(
        settled((5, "dave"), (9, "alice")),
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 1, recipient_id=5),),
    )
    assert planned(users, actual).actions == (
        HandOverDocument(document_id=40, user_id=5, username="alice"),
    )


def test_a_document_that_carries_the_name_of_its_owner_needs_no_action():
    actual = replace(
        settled((5, "alice")),
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 5, recipient_id=None),),
    )
    assert planned([member(5, "alice")], actual).actions == ()


def test_a_document_without_owner_or_account_is_deleted():
    actual = state(
        token_user=ADMIN,
        documents=(
            CredentialDocument(40, "alice", None, recipient_id=None),
            CredentialDocument(41, "bob", 1, recipient_id=6),
            CredentialDocument(42, "carol", 9, recipient_id=None),
        ),
    )
    assert planned([], actual).actions == (
        DeleteDocument(document_id=40),
        DeleteDocument(document_id=41),
    )


def test_a_removal_takes_the_documents_of_its_account_only():
    # paperless has no user 5 any more, user 6 left the group, and user 9 stays
    users = [former(6, "bob"), member(9, "zoe")]
    actual = replace(
        settled((5, "alice"), (6, "bob"), (9, "zoe")),
        token_user=ADMIN,
        documents=(
            CredentialDocument(40, "alice", None, recipient_id=None),
            CredentialDocument(41, "bob", 6, recipient_id=None),
            CredentialDocument(42, "bob", 1, recipient_id=6),
            CredentialDocument(43, "zoe", 9, recipient_id=None),
        ),
    )
    assert planned(users, actual).actions == (
        WriteMap(("!u20009 = zoe", "!nobody = *")),
        Deprovision(
            account="u20005",
            user_id=5,
            login_name="alice",
            workflow_id=105,
            delete_folder=True,
            documents=(40,),
        ),
        Deprovision(
            account="u20006",
            user_id=6,
            login_name="bob",
            workflow_id=106,
            delete_folder=True,
            documents=(41, 42),
        ),
    )


def test_the_removal_of_the_token_user_leaves_the_uploads_for_other_users():
    # the user of the token is in the sync group and leaves it
    users = [former(1, "admin"), member(6, "bob")]
    actual = replace(
        settled((1, "admin"), (6, "bob")),
        token_user=ADMIN,
        documents=(
            CredentialDocument(40, "admin", 1, recipient_id=None),
            CredentialDocument(41, "bob", 1, recipient_id=6),
        ),
    )
    assert planned(users, actual).actions == (
        WriteMap(("!u20006 = bob", "!nobody = *")),
        Deprovision(
            account="u20001",
            user_id=1,
            login_name="admin",
            workflow_id=101,
            delete_folder=True,
            documents=(40,),
        ),
        HandOverDocument(document_id=41, user_id=6, username="bob"),
    )


def test_a_waiting_document_of_another_owner_is_left_with_a_warning():
    actual = replace(
        settled((5, "alice")),
        token_user=ADMIN,
        documents=(CredentialDocument(40, "alice", 9, recipient_id=5),),
    )
    plan = planned([member(5, "alice")], actual)
    assert plan.actions == ()
    assert any("40" in w for w in plan.warnings)
