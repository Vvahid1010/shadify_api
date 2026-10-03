"""Small private domain projection; artist authority is repository-owned and membership-scoped."""
from typing import Protocol
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .media import MediaError


class TrackDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid title")
        return value


class DomainRepository(Protocol):
    def project_user(self, account_id: str) -> dict: ...
    def create_track(self, account_id: str, artist_id: UUID, title: str) -> dict | None: ...
    def list_tracks(self, account_id: str, artist_id: UUID, limit: int = 20, cursor: UUID | None = None) -> dict | None: ...


class DomainService:
    def __init__(self, repository: DomainRepository):
        self.repository = repository

    def me(self, account_id: str):
        return self.repository.project_user(account_id)

    def create_track(self, account_id: str, artist_id: UUID, draft: TrackDraft):
        result = self.repository.create_track(account_id, artist_id, draft.title)
        if result is None:
            raise MediaError(404, "Artist not found")
        return result

    def tracks(self, account_id: str, artist_id: UUID, limit: int = 20, cursor: UUID | None = None):
        result = self.repository.list_tracks(account_id, artist_id, limit, cursor)
        if result is None:
            raise MediaError(404, "Artist not found")
        return result
