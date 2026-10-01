"""PostgreSQL metadata only; caller supplies a connection factory, never browser input."""
from psycopg.rows import dict_row
from .media import Asset


class PostgresMediaRepository:
    def __init__(self, connect):
        self.connect = connect

    def owns_track(self, owner, artist, track):
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM shadify_tracks t JOIN shadify_artists a ON a.id=t.artist_id "
                "WHERE t.id=%s AND a.id=%s AND a.owner_id=%s", (track, artist, owner)
            ).fetchone() is not None

    def insert(self, asset):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO shadify_media_assets "
                "(id,owner_id,artist_id,track_id,key,asset_type,mime,size,expires_at,storage_provider,state,access_class,publish_state) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                tuple(getattr(asset, name) for name in Asset.__dataclass_fields__),
            )

    def get(self, asset_id):
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute("SELECT * FROM shadify_media_assets WHERE id=%s", (asset_id,))
                row = cursor.fetchone()
                return Asset(**row) if row else None

    def transition(self, asset_id, expected, target):
        with self.connect() as connection:
            return connection.execute(
                "UPDATE shadify_media_assets SET state=%s WHERE id=%s AND state=%s",
                (target, asset_id, expected),
            ).rowcount == 1
