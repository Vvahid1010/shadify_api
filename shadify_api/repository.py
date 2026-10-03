"""PostgreSQL metadata; all management authority comes from ArtistAccessRepository."""
from uuid import uuid4
from psycopg.rows import dict_row
from .access import ArtistAccessRepository
from .artists import ArtistOperations
from .media import Asset, MediaError


class PostgresMediaRepository(ArtistAccessRepository, ArtistOperations):
    def __init__(self, connect):
        self.connect=connect

    def owns_track(self,owner,artist,track):
        try:
            with self.authorized(owner,artist) as (c,_,_,_):
                return c.execute('SELECT 1 FROM shadify_tracks WHERE id=%s AND artist_id=%s',(track,artist)).fetchone() is not None
        except MediaError as e:
            if e.status in (403,404): return False
            raise

    def insert(self,asset):
        with self.authorized(asset.owner_id,asset.artist_id) as (c,_,_,_):
            if c.execute('SELECT 1 FROM shadify_tracks WHERE id=%s AND artist_id=%s',(asset.track_id,asset.artist_id)).fetchone() is None:
                raise MediaError(404,'Track not found')
            c.execute('INSERT INTO shadify_media_assets '
                '(id,owner_id,artist_id,track_id,key,asset_type,mime,size,expires_at,storage_provider,state,access_class,publish_state) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                tuple(getattr(asset,name) for name in Asset.__dataclass_fields__))

    def get(self,asset_id):
        # Internal locator only; public/private routes use get_for_actor instead.
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                row=c.execute('SELECT * FROM shadify_media_assets WHERE id=%s',(asset_id,)).fetchone()
                return Asset(**row) if row else None

    def get_for_actor(self,actor,asset_id):
        asset=self.get(asset_id)
        if asset is None: return None
        with self.authorized(actor,asset.artist_id) as (c,_,_,_):
            row=c.execute('SELECT * FROM shadify_media_assets WHERE id=%s AND artist_id=%s',(asset_id,asset.artist_id)).fetchone()
            return Asset(**row) if row else None

    def transition(self,actor,asset_id,expected,target):
        if (expected,target) not in {('upload_pending','uploaded'),('upload_pending','failed')}:
            raise ValueError('Only original upload completion transitions are supported')
        asset=self.get(asset_id)
        if asset is None: raise MediaError(404,'Asset not found')
        with self.authorized(actor,asset.artist_id) as (c,_,_,_):
            return c.execute('UPDATE shadify_media_assets SET state=%s WHERE id=%s AND artist_id=%s AND state=%s '
                "AND expires_at>clock_timestamp() AND asset_type IN ('audio_original','artwork_original')",
                (target,asset_id,asset.artist_id,expected)).rowcount==1

    def access_context(self,actor,artist):
        with self.authorized(actor,artist) as (_,auth,_,_): return auth.dto()

    def media_metadata(self,actor,asset_id):
        asset=self.get(asset_id)
        if asset is None: raise MediaError(404,'Asset not found')
        with self.authorized(actor,asset.artist_id) as (c,auth,_,_):
            row=c.execute('SELECT * FROM shadify_media_assets WHERE id=%s AND artist_id=%s',(asset_id,asset.artist_id)).fetchone()
            if row is None: raise MediaError(404,'Asset not found')
            return {**auth.dto(),**{k:(str(row[k]) if k in ('artist_id','track_id') else row[k])
                for k in ('artist_id','track_id','asset_type','mime','size','state','access_class','publish_state')},'asset_id':str(asset_id)}

    def project_user(self,account_id):
        # Admission wrapper owns provenance; this read cannot create or verify an identity.
        with self.authorized(account_id) as (c,auth,_,_):
            artists=c.execute('SELECT artist_id FROM artist_memberships WHERE user_id=%s AND revoked_at IS NULL ORDER BY artist_id',
                              (auth.user_id,)).fetchall()
            return {'user_id':str(auth.user_id),'account_id':account_id,'artist_ids':[str(r['artist_id']) for r in artists]}

    def create_track(self,account_id,artist_id,title):
        result=self.content_write(account_id,artist_id,'tracks',{'title':title})
        content=result.pop('content')
        return {**result,'track_id':content['id'],'title':content['title'],'publish_state':content['publish_state']}

    def list_tracks(self,account_id,artist_id,limit=20,cursor=None):
        result=self.content_read(account_id,artist_id,'tracks',limit=limit,cursor=cursor)
        result['tracks']=result.pop('items')
        return result

    def ready(self):
        # Read-only compatibility check for the matching 001..005 schema; no auto-migration.
        with self.connect() as c:
            c.execute('SELECT u.first_admitted_at,u.active,a.slug,a.publish_state,t.delivery_asset_id,m.storage_provider, '
                      'am.revoked_at,g.revoked_at,au.actor_account_id,r.data,v.data,mo.data,e.data '
                      'FROM shadify_users u,shadify_artists a,shadify_tracks t,shadify_media_assets m, '
                      'artist_memberships am,shadify_admin_grants g,artist_ownership_audit au,shadify_releases r, '
                      'shadify_videos v,shadify_moments mo,shadify_events e LIMIT 0')
        return True
