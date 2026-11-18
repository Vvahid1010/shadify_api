"""Small private domain projection; artist authority is repository-owned and membership-scoped."""
from typing import Protocol
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .media import MediaError
from .auth import Identity, require_fresh_actor


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
    def project_user(self, actor: Identity) -> dict: ...
    def create_track(self, actor: Identity, artist_id: UUID, title: str) -> dict | None: ...
    def list_tracks(self, actor: Identity, artist_id: UUID, limit: int = 20, cursor: UUID | None = None) -> dict | None: ...


class DomainService:
    def __init__(self, repository: DomainRepository):
        self.repository = repository

    def me(self, actor: Identity):
        require_fresh_actor(actor)
        return self.repository.project_user(actor)

    def create_track(self, actor: Identity, artist_id: UUID, draft: TrackDraft):
        require_fresh_actor(actor)
        result = self.repository.create_track(actor, artist_id, draft.title)
        if result is None:
            raise MediaError(404, "Artist not found")
        return result

    def tracks(self, actor: Identity, artist_id: UUID, limit: int = 20, cursor: UUID | None = None):
        require_fresh_actor(actor)
        result = self.repository.list_tracks(actor, artist_id, limit, cursor)
        if result is None:
            raise MediaError(404, "Artist not found")
        return result
