"""Read-only automatic mood playlists; no route enrollment or playback URL issuance."""
from uuid import UUID
from psycopg.rows import dict_row
from .artist_models import MOOD_IDS
from .delivery import PlaybackTarget, require_playable
from .media import MediaError

MOOD_LABELS = {
    'calm': {'en': 'Calm', 'fa': 'آرام'},
    'romantic': {'en': 'Romantic', 'fa': 'عاشقانه'},
    'happy': {'en': 'Happy', 'fa': 'شاد'},
    'party': {'en': 'Party', 'fa': 'مهمانی'},
}
# Bound readiness/approval work per request; cursor advances past inspected candidates.
CANDIDATE_LIMIT = 100


def _playlist(repository, cursor, mood_id, limit, after):
    rows = cursor.execute(
        "SELECT t.*, a.name AS artist_name FROM shadify_tracks t "
        "JOIN shadify_artists a ON a.id=t.artist_id "
        "WHERE a.publish_state='published' AND t.publish_state='published' "
        "AND jsonb_typeof(t.data->'mood_ids')='array' AND (t.data->'mood_ids') ? %s "
        "AND (%s::uuid IS NULL OR t.id>%s) ORDER BY t.id LIMIT %s",
        (mood_id, after, after, CANDIDATE_LIMIT + 1)).fetchall()
    tracks = []
    last = None
    inspected = 0
    for row in rows[:CANDIDATE_LIMIT]:
        inspected += 1
        last = row['id']
        if not repository._publishable(cursor, row['artist_id'], 'tracks', row, lock=False):
            continue
        preview = row['access_class'] == 'paid'
        asset_id = row['preview_asset_id'] if preview else row['delivery_asset_id']
        try:
            asset = repository._asset(cursor, row['artist_id'], asset_id,
                'audio_preview' if preview else 'audio_delivery', track_id=row['id'], lock=False)
            if asset is None:
                continue
            from .media import Asset
            require_playable(PlaybackTarget(row['id'], row['publish_state'],
                row['access_class'], Asset(**asset), 'published'), row['id'], preview=preview)
        except MediaError as error:
            if error.status in (403, 404, 409):
                continue
            raise
        labels = row['data'].get('mood_ids', [])
        tracks.append({
            'id': str(row['id']), 'artist_id': str(row['artist_id']),
            'artist_name': row['artist_name'], 'title': row['title'],
            'publish_state': 'published', 'access_class': row['access_class'],
            'mood_ids': [label for label in MOOD_IDS if label in labels],
            'preview_asset_id' if preview else 'delivery_asset_id': str(asset_id),
        })
        if len(tracks) == limit:
            break
    return {
        'id': 'auto-mood-' + mood_id, 'mood_id': mood_id,
        'labels': MOOD_LABELS[mood_id], 'kind': 'automatic',
        'track_ids': [track['id'] for track in tracks], 'tracks': tracks,
        'next_cursor': str(last) if last is not None and inspected < len(rows) else None,
    }


def discover_moods(repository, *, mood_id=None, limit=20, cursor=None):
    """One bounded ordered playlist per mood; empty groups keep the rail shell visible.

    Optional mood_id/cursor retrieves another page of that same automatic playlist.
    Existing curated playlists are neither read nor rewritten. Paid tracks use only
    a separate approved free preview; playback authorization is still checked on play.
    """
    if (mood_id is not None and mood_id not in MOOD_IDS
            or type(limit) is not int or not 1 <= limit <= 20
            or cursor is not None and (mood_id is None or not isinstance(cursor, UUID))):
        raise MediaError(422, 'Invalid mood playlist request')
    with repository.connect() as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        with connection.cursor(row_factory=dict_row) as query:
            return {'playlists': [_playlist(repository, query, mood, limit, cursor)
                for mood in ((mood_id,) if mood_id is not None else MOOD_IDS)]}
