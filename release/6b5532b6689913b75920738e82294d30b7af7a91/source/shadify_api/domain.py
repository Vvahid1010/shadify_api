"""Small private domain projection; artist eligibility remains an existing relationship."""
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
    def create_track(self, account_id: str, artist_id: UUID, title: str) -> UUID | None: ...
    def list_tracks(self, account_id: str, artist_id: UUID) -> list[dict] | None: ...


class DomainService:
    def __init__(self, repository: DomainRepository):
        self.repository = repository

    def me(self, account_id: str):
        return self.repository.project_user(account_id)

    def create_track(self, account_id: str, artist_id: UUID, draft: TrackDraft):
        track_id = self.repository.create_track(account_id, artist_id, draft.title)
        if track_id is None:
            raise MediaError(403, "Artist ownership required")
        return {"track_id": str(track_id), "artist_id": str(artist_id),
                "title": draft.title, "publish_state": "draft"}

    def tracks(self, account_id: str, artist_id: UUID):
        tracks = self.repository.list_tracks(account_id, artist_id)
        if tracks is None:
            raise MediaError(403, "Artist ownership required")
        return {"tracks": tracks}
