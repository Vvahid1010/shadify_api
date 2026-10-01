"""Original upload intents only. Delivery, publishing and processing remain separate."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .storage import ObjectStorage


class MediaError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


class UploadIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artist_id: UUID
    track_id: UUID
    asset_type: Literal["audio_original", "artwork_original"]
    mime: Literal["audio/wav", "audio/flac", "image/jpeg", "image/png"]
    size: int = Field(gt=0)


@dataclass(frozen=True)
class Asset:
    id: UUID
    owner_id: str
    artist_id: UUID
    track_id: UUID
    key: str
    asset_type: str
    mime: str
    size: int
    expires_at: datetime
    storage_provider: str = "r2"
    state: str = "upload_pending"
    access_class: str = "private"
    publish_state: str = "draft"


class MediaRepository(Protocol):
    def owns_track(self, owner: str, artist: UUID, track: UUID) -> bool: ...
    def insert(self, asset: Asset) -> None: ...
    def get(self, asset_id: UUID) -> Asset | None: ...
    def transition(self, asset_id: UUID, expected: str, target: str) -> bool: ...


def valid_signature(mime, data):
    return {
        "audio/wav": data[:4] == b"RIFF" and data[8:12] == b"WAVE",
        "audio/flac": data[:4] == b"fLaC",
        "image/jpeg": data[:3] == b"\xff\xd8\xff",
        "image/png": data[:8] == b"\x89PNG\r\n\x1a\n",
    }.get(mime, False)


class MediaService:
    def __init__(self, repository: MediaRepository, storage: ObjectStorage, max_size: int, ttl: int):
        self.repository, self.storage = repository, storage
        self.max_size, self.ttl = max_size, ttl

    def create(self, owner: str, intent: UploadIntent):
        if not self.repository.owns_track(owner, intent.artist_id, intent.track_id):
            raise MediaError(403, "Artist/track ownership required")
        allowed = {"audio_original": {"audio/wav", "audio/flac"},
                   "artwork_original": {"image/jpeg", "image/png"}}
        if intent.mime not in allowed[intent.asset_type] or intent.size > self.max_size:
            raise MediaError(422, "Unsupported type or configured size limit exceeded")
        asset_id = uuid4()
        key = f"originals/artists/{intent.artist_id}/tracks/{intent.track_id}/{asset_id}/master"
        authorization = self.storage.authorize_upload(key, intent.mime, intent.size)
        asset = Asset(asset_id, owner, intent.artist_id, intent.track_id, key,
                      intent.asset_type, intent.mime, intent.size,
                      datetime.now(timezone.utc) + timedelta(seconds=self.ttl))
        self.repository.insert(asset)
        return asset, authorization

    def complete(self, owner: str, asset_id: UUID):
        asset = self.repository.get(asset_id)
        if asset is None or asset.owner_id != owner:
            raise MediaError(404, "Asset not found")
        if not self.repository.owns_track(owner, asset.artist_id, asset.track_id):
            raise MediaError(403, "Artist/track ownership required")
        if asset.state == "uploaded":
            return asset
        if asset.state != "upload_pending":
            raise MediaError(409, "Upload cannot be completed")
        if asset.expires_at <= datetime.now(timezone.utc):
            raise MediaError(409, "Upload session expired; request a new asset")
        info = self.storage.inspect(asset.key)
        if (info.size != asset.size or info.content_type != asset.mime
                or not valid_signature(asset.mime, info.prefix)):
            self.repository.transition(asset.id, "upload_pending", "failed")
            raise MediaError(422, "Object size, MIME or container signature mismatch")
        if not self.repository.transition(asset.id, "upload_pending", "uploaded"):
            raise MediaError(409, "Upload state changed; retry")
        return replace(asset, state="uploaded")
