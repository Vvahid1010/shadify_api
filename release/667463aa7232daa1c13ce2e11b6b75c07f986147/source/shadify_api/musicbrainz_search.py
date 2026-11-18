"""Manual public-metadata lookup via the existing coordinated provider.

Inactive until the runtime owner explicitly installs this route and grants it.
No ingest decision, media attachment or track write happens here.
"""
import time
from uuid import UUID
from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .auth import Identity, require_fresh_actor
from .musicbrainz import ProviderUnavailable, query_metadata, text
from .musicbrainz_queue import QueuedMusicBrainzProvider


class RecordingSearchQuery(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(min_length=1, max_length=120)

    @field_validator('title', 'artist')
    @classmethod
    def clean(cls, value):
        result = text(value, 200)
        if not result:
            raise ValueError('Public title and artist required')
        return result


def search_recordings(repository, provider, actor, artist_id, track_id, body):
    def authority():
        require_fresh_actor(actor)
        data = (repository.content_read(actor, artist_id, 'tracks', track_id)
                if track_id is not None else repository.private_page(actor, artist_id))
        if (data.get('artist_id') != str(artist_id)
                or (track_id is not None and data.get('content', {}).get('id') != str(track_id))
                or data.get('capabilities', {}).get('manage_content') is not True):
            raise HTTPException(403, 'Track editing permission required')
        return data

    before = authority()
    # duration=1 is the queue's neutral metadata-search convention, never a
    # claimed duration. With no ISRC the existing snapshot cannot auto-match.
    query = query_metadata(dict(title=body.title, artist=body.artist, isrc='', duration_ms=1))
    result = None
    try:
        result = provider.lookup(query, deadline=time.monotonic() + 12)
    except ProviderUnavailable:
        pass
    # Recheck current permission/session after any provider wait, including error.
    after = authority()
    if after.get('actor_account_id') != before.get('actor_account_id'):
        raise HTTPException(403, 'Track editing context changed')
    if result is None:
        raise HTTPException(503, 'MusicBrainz unavailable; refine query or retry')
    if (result.get('provider') != 'musicbrainz' or result.get('complete') is not True
            or result.get('truncated') is not False or not isinstance(result.get('candidates'), list)
            or len(result['candidates']) > 5 or result.get('total') != len(result['candidates'])):
        raise HTTPException(503, 'Complete MusicBrainz result unavailable')
    return dict(actor_account_id=after['actor_account_id'], artist_id=str(artist_id),
                track_id=str(track_id) if track_id is not None else None, provider='musicbrainz', complete=True, truncated=False,
                total=result['total'], candidates=[{key: row[key] for key in ('mbid', 'title', 'artist', 'duration_ms')}
                                                 for row in result['candidates']],
                fetched_at=result['fetched_at'], snapshot_sha256=result['snapshot_sha256'])


def install_musicbrainz_search_route(app, identity, repository, provider):
    if provider is not None and not isinstance(provider, QueuedMusicBrainzProvider):
        raise ValueError('Existing coordinated MusicBrainz provider required')

    @app.post('/api/artists/{artist_id}/musicbrainz-search')
    def draft_search(artist_id: UUID, body: RecordingSearchQuery,
                     response: Response, actor: Identity = Depends(identity)):
        response.headers['Cache-Control'] = 'no-store'
        if repository is None or provider is None:
            raise HTTPException(503, 'Manual metadata search integration pending')
        return search_recordings(repository, provider, actor, artist_id, None, body)

    @app.post('/api/artists/{artist_id}/tracks/{track_id}/musicbrainz-search')
    def search(artist_id: UUID, track_id: UUID, body: RecordingSearchQuery,
               response: Response, actor: Identity = Depends(identity)):
        response.headers['Cache-Control'] = 'no-store'
        if repository is None or provider is None:
            raise HTTPException(503, 'Manual metadata search integration pending')
        return search_recordings(repository, provider, actor, artist_id, track_id, body)
