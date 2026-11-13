from collections.abc import Mapping
from dataclasses import dataclass


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
    uid: int | None
    enabled: bool


@dataclass(frozen=True)
class ManagedAccount:
    username: str
    uid: int
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
    documents: tuple[CredentialDocument, ...]
    token_user: TokenUser | None


@dataclass(frozen=True)
class CreateAccount:
    user: PaperlessUser
    uid: int
    upload: bool


@dataclass(frozen=True)
class CreateFolder:
    username: str
    uid: int


@dataclass(frozen=True)
class CreateWorkflow:
    user_id: int
    username: str


@dataclass(frozen=True)
class DeleteDocument:
    document_id: int


@dataclass(frozen=True)
class DeleteWorkflow:
    workflow_id: int


@dataclass(frozen=True)
class Deprovision:
    username: str
    user_id: int
    workflow_id: int | None
    delete_folder: bool
    documents: tuple[int, ...] | None


@dataclass(frozen=True)
class DisableAccount:
    username: str


@dataclass(frozen=True)
class EnableAccount:
    username: str


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
class RenameUser:
    old_username: str
    new_username: str
    uid: int
    user_id: int
    workflow_id: int | None


@dataclass(frozen=True)
class RepairUnixAccount:
    username: str
    uid: int


@dataclass(frozen=True)
class UpdateWorkflow:
    workflow_id: int
    user_id: int
    username: str


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
    | RenameUser
    | RepairUnixAccount
    | UpdateWorkflow
)


@dataclass(frozen=True)
class Plan:
    actions: tuple[Action, ...]
    warnings: tuple[str, ...]


def workflow_filter(consume_path: str, username: str) -> str:
    # paperless matches the filter with fnmatch against the absolute path of
    # the scan, and * there also matches /. Anchored at the consume path, it
    # cannot match a folder of that name inside the inbox of another user.
    return f"{consume_path}/{username}/*"
