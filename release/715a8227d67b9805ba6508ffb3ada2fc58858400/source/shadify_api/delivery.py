"""Prepared read-only playback boundary; no HTTP/schema/purchase wiring yet.

Only authoritative repository projections may supply PlaybackTarget. These
values are not browser request fields. No current draft original is playable.
"""
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from .media import Asset, MediaError
from .storage import MediaDeliveryProvider, PlaybackAccess


@dataclass(frozen=True)
class PlaybackTarget:
    track_id: UUID
    track_publish_state: str
    track_access_class: str
    asset: Asset


class PlaybackRepository(Protocol):
    def get_playback_target(self, track_id: UUID, *, preview: bool) -> PlaybackTarget | None: ...


class PlaybackService:
    def __init__(self, repository: PlaybackRepository, provider: MediaDeliveryProvider):
        self.repository, self.provider = repository, provider

    def create_playback_access(self, track_id: UUID, *, preview: bool = False,
                               account_id: str | None = None) -> PlaybackAccess:
        target = self.repository.get_playback_target(track_id, preview=preview)
        if target is None:
            raise MediaError(404, "Playable media not found")
        asset = target.asset
        kind, prefix = ("audio_preview", "previews/") if preview else ("audio_delivery", "delivery/")
        if (target.track_id != track_id or asset.track_id != track_id
                or target.track_publish_state != "published" or asset.publish_state != "published"
                or asset.state != "ready" or asset.asset_type != kind or not asset.key.startswith(prefix)
                or asset.storage_provider != "r2"):
            raise MediaError(404, "Playable media not found")
        # Guest free playback needs no account. Full paid playback remains
        # closed even for a signed-in account until existing entitlement proof
        # is wired; an account or a preview is never proof of purchase.
        if ((not preview and target.track_access_class != "free")
                or target.track_access_class not in {"free", "paid"} or asset.access_class != "free"):
            raise MediaError(403, "Playback entitlement unavailable")
        return self.provider.create_playback_access(asset.key)
