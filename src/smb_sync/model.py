import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# The line gives each name without a line of its own to nobody, so that Samba
# refuses a login under an account name and under each unknown name.
MAP_LAST_LINE = "!nobody = *"

_MAP_LINE = re.compile(r"!(u[0-9]+) = (.+)")
_MARK = re.compile(r"smb-sync uid=([0-9]+) joined=([0-9]+)")


@dataclass(frozen=True)
class PaperlessUser:
    id: int
    username: str
    is_active: bool
    joined: int
    member: bool


@dataclass(frozen=True)
class SambaAccount:
    username: str
    description: str
    enabled: bool


@dataclass(frozen=True)
class ManagedAccount:
    name: str
    uid: int
    joined: int
    enabled: bool


@dataclass(frozen=True)
class InboxFolder:
    name: str
    uid: int


@dataclass(frozen=True)
class ManagedWorkflow:
    id: int
    name: str
    defined_for: int | None


@dataclass(frozen=True)
class CredentialDocument:
    id: int
    username: str
    owner_id: int | None
    recipient_id: int | None


@dataclass(frozen=True)
class TokenUser:
    id: int
    is_superuser: bool


@dataclass(frozen=True)
class ActualState:
    accounts: tuple[SambaAccount, ...]
    folders: tuple[InboxFolder, ...]
    workflows: tuple[ManagedWorkflow, ...]
    unix_uids: Mapping[str, int]
    map_lines: tuple[str, ...]
    documents: tuple[CredentialDocument, ...]
    token_user: TokenUser | None


@dataclass(frozen=True)
class CreateAccount:
    user: PaperlessUser
    uid: int
    upload: bool


@dataclass(frozen=True)
class CreateFolder:
    account: str
    uid: int


@dataclass(frozen=True)
class CreateWorkflow:
    user_id: int
    account: str


@dataclass(frozen=True)
class DeleteDocument:
    document_id: int


@dataclass(frozen=True)
class DeleteWorkflow:
    workflow_id: int


@dataclass(frozen=True)
class Deprovision:
    account: str
    user_id: int
    login_name: str | None
    workflow_id: int | None
    delete_folder: bool
    documents: tuple[int, ...] | None


@dataclass(frozen=True)
class DisableAccount:
    account: str


@dataclass(frozen=True)
class EnableAccount:
    account: str


@dataclass(frozen=True)
class HandOverDocument:
    document_id: int
    user_id: int
    username: str


@dataclass(frozen=True)
class RenameDocument:
    document_id: int
    old_username: str
    new_username: str


@dataclass(frozen=True)
class RepairUnixAccount:
    account: str
    uid: int


@dataclass(frozen=True)
class UpdateWorkflow:
    workflow_id: int
    user_id: int
    account: str


@dataclass(frozen=True)
class WriteMap:
    lines: tuple[str, ...]


Action = (
    CreateAccount
    | CreateFolder
    | CreateWorkflow
    | DeleteDocument
    | DeleteWorkflow
    | Deprovision
    | DisableAccount
    | EnableAccount
    | HandOverDocument
    | RenameDocument
    | RepairUnixAccount
    | UpdateWorkflow
    | WriteMap
)


@dataclass(frozen=True)
class Plan:
    actions: tuple[Action, ...]
    warnings: tuple[str, ...]
    logins: Mapping[str, str]


def account_name(uid: int) -> str:
    return f"u{uid}"


def entry_mark(uid: int, joined: int) -> str:
    return f"smb-sync uid={uid} joined={joined}"


def login_key(name: str) -> tuple[str, ...]:
    # Samba compares two names character by character and without case, and the
    # casefold() form of a whole name can be equal for two names that differ in
    # one place, such as ßs and sß.
    return tuple(c.casefold() for c in name)


def map_line(account: str, login: str) -> str:
    return f"!{account} = {login}"


def map_logins(lines: Iterable[str]) -> dict[str, str]:
    matches = (_MAP_LINE.fullmatch(line) for line in lines)
    return {match[1]: match[2] for match in matches if match is not None}


def parse_mark(description: str) -> tuple[int, int] | None:
    match = _MARK.fullmatch(description)
    if match is None:
        return None
    return int(match[1]), int(match[2])


def workflow_filter(consume_path: str, account: str) -> str:
    # paperless matches the filter with fnmatch against the absolute path of
    # the scan, and * there also matches /. Anchored at the consume path, it
    # cannot match a folder of that name inside the inbox of another user.
    return f"{consume_path}/{account}/*"
