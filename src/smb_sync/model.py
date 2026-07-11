from dataclasses import dataclass


@dataclass(frozen=True)
class PaperlessUser:
    id: int
    username: str
    is_active: bool


@dataclass(frozen=True)
class SambaAccount:
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
    username: str


@dataclass(frozen=True)
class ActualState:
    accounts: tuple[SambaAccount, ...]
    folders: tuple[InboxFolder, ...]
    workflows: tuple[ManagedWorkflow, ...]


@dataclass(frozen=True)
class CreateAccount:
    user: PaperlessUser
    uid: int


@dataclass(frozen=True)
class CreateFolder:
    username: str
    uid: int


@dataclass(frozen=True)
class CreateWorkflow:
    user_id: int
    username: str


@dataclass(frozen=True)
class DeleteWorkflow:
    workflow_id: int


@dataclass(frozen=True)
class DeleteAccount:
    username: str


@dataclass(frozen=True)
class DeleteFolder:
    username: str


@dataclass(frozen=True)
class DisableAccount:
    username: str


@dataclass(frozen=True)
class EnableAccount:
    username: str


@dataclass(frozen=True)
class RenameUser:
    old_username: str
    new_username: str
    uid: int
    user_id: int
    workflow_id: int | None


Action = (
    CreateAccount
    | CreateFolder
    | CreateWorkflow
    | DeleteWorkflow
    | DeleteAccount
    | DeleteFolder
    | DisableAccount
    | EnableAccount
    | RenameUser
)


@dataclass(frozen=True)
class Plan:
    actions: tuple[Action, ...]
    warnings: tuple[str, ...]
