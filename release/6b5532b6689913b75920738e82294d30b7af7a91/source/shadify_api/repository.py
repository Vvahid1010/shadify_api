"""PostgreSQL metadata only; caller supplies a connection factory, never browser input."""
from psycopg.rows import dict_row
from uuid import uuid4
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
            result = connection.execute(
                "INSERT INTO shadify_media_assets "
                "(id,owner_id,artist_id,track_id,key,asset_type,mime,size,expires_at,storage_provider,state,access_class,publish_state) "
                "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s "
                "FROM shadify_tracks t JOIN shadify_artists a ON a.id=t.artist_id "
                "WHERE t.id=%s AND a.id=%s AND a.owner_id=%s",
                tuple(getattr(asset, name) for name in Asset.__dataclass_fields__) +
                (asset.track_id, asset.artist_id, asset.owner_id),
            )
            if result.rowcount != 1:
                from .media import MediaError
                raise MediaError(403, "Artist/track ownership required")

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


    def project_user(self, account_id):
        with self.connect() as connection:
            row = connection.execute(
                "INSERT INTO shadify_users (id,auth_account_id) VALUES (%s,%s) "
                "ON CONFLICT (auth_account_id) DO UPDATE SET auth_account_id=EXCLUDED.auth_account_id "
                "RETURNING id", (uuid4(), account_id),
            ).fetchone()
            artists = connection.execute(
                "SELECT id FROM shadify_artists WHERE owner_id=%s ORDER BY id", (account_id,),
            ).fetchall()
            return {"user_id": str(row[0]), "account_id": account_id,
                    "artist_ids": [str(item[0]) for item in artists]}

    def create_track(self, account_id, artist_id, title):
        # Ownership and insert share one SQL statement; no client-selected owner/track ID.
        track_id = uuid4()
        with self.connect() as connection:
            row = connection.execute(
                "INSERT INTO shadify_tracks (id,artist_id,title) "
                "SELECT %s,id,%s FROM shadify_artists WHERE id=%s AND owner_id=%s "
                "RETURNING id", (track_id, title, artist_id, account_id),
            ).fetchone()
            return row[0] if row else None

    def list_tracks(self, account_id, artist_id):
        with self.connect() as connection:
            owner = connection.execute(
                "SELECT 1 FROM shadify_artists WHERE id=%s AND owner_id=%s", (artist_id, account_id),
            ).fetchone()
            if owner is None:
                return None
            rows = connection.execute(
                "SELECT id,title FROM shadify_tracks WHERE artist_id=%s ORDER BY id", (artist_id,),
            ).fetchall()
            return [{"track_id": str(row[0]), "title": row[1], "publish_state": "draft"} for row in rows]

    def ready(self):
        # Read-only schema compatibility check; never migrate or provision on startup.
        with self.connect() as connection:
            connection.execute(
                "SELECT u.id,u.auth_account_id,a.owner_id,t.title,m.state,m.storage_provider "
                "FROM shadify_users u,shadify_artists a,shadify_tracks t,shadify_media_assets m LIMIT 0"
            )
        return True
