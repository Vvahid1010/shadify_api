"""PostgreSQL metadata; all management authority comes from ArtistAccessRepository."""
from uuid import uuid4
from contextlib import contextmanager
from datetime import datetime, timezone
from psycopg.rows import dict_row
from .access import ArtistAccessRepository
from .artists import ArtistOperations
from .ingest import IngestRepository
from .ingest_review import IngestReviewRepository
from .studio_uploads import StudioUploadsRepository
from .media import Asset, MediaError
from .auth import require_fresh_actor


class PostgresMediaRepository(ArtistAccessRepository, ArtistOperations, IngestRepository, IngestReviewRepository, StudioUploadsRepository):
    def __init__(self, connect):
        self.connect=connect

    def owns_track(self,owner,artist,track):
        try:
            with self.authorized(owner,artist,operation='manage_content',audit=False) as (c,_,_,_):
                return c.execute('SELECT 1 FROM shadify_tracks WHERE id=%s AND artist_id=%s',(track,artist)).fetchone() is not None
        except MediaError as e:
            if e.status in (403,404): return False
            raise

    def insert(self,actor,asset):
        with self.authorized(actor,asset.artist_id,operation='manage_content') as (c,_,_,_):
            if asset.owner_id != actor.account_id:
                raise MediaError(422,'Uploader audit identity mismatch')
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
        require_fresh_actor(actor)
        asset=self.get(asset_id)
        if asset is None: return None
        with self.authorized(actor,asset.artist_id,operation='manage_content',audit=False) as (c,_,_,_):
            row=c.execute('SELECT * FROM shadify_media_assets WHERE id=%s AND artist_id=%s',(asset_id,asset.artist_id)).fetchone()
            return Asset(**row) if row else None

    def transition(self,actor,asset_id,expected,target,*,byte_sha256=None):
        require_fresh_actor(actor)
        if (expected,target) not in {('upload_pending','uploaded'),('upload_pending','failed')}:
            raise ValueError('Only original upload completion transitions are supported')
        asset=self.get(asset_id)
        if asset is None: raise MediaError(404,'Asset not found')
        with self.authorized(actor,asset.artist_id,operation='manage_content') as (c,_,_,_):
            changed=c.execute('UPDATE shadify_media_assets SET state=%s WHERE id=%s AND artist_id=%s AND state=%s '
                "AND expires_at>clock_timestamp() AND asset_type IN ('audio_original','artwork_original')",
                (target,asset_id,asset.artist_id,expected)).rowcount==1
            if changed and target=='uploaded':self.enqueue_ingest(c,asset_id,byte_sha256)
            return changed

    @contextmanager
    def upload_guard(self, actor, asset):
        with self.authorized(actor, asset.artist_id, operation='manage_content') as (c, _, _, _):
            row = c.execute('SELECT * FROM shadify_media_assets WHERE id=%s FOR UPDATE', (asset.id,)).fetchone()
            if (row is None or Asset(**row) != asset or row['state'] != 'upload_pending'
                    or row['expires_at'] <= datetime.now(timezone.utc)):
                raise MediaError(409, 'Upload state changed or expired')
            if c.execute('SELECT 1 FROM shadify_tracks WHERE id=%s AND artist_id=%s',
                         (asset.track_id, asset.artist_id)).fetchone() is None:
                raise MediaError(404, 'Track not found')
            yield

    def check_upload(self, actor, asset):
        with self.upload_guard(actor, asset): pass

    def get_playback_target(self, track_id, *, preview=False):
        from .delivery import PlaybackTarget
        reference = 'preview_asset_id' if preview else 'delivery_asset_id'
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                row = c.execute('SELECT t.id,t.artist_id,t.publish_state,t.access_class,t.' + reference +
                                ' AS asset_id,a.publish_state AS page_state FROM shadify_tracks t '
                                'JOIN shadify_artists a ON a.id=t.artist_id WHERE t.id=%s', (track_id,)).fetchone()
                if row is None or row['asset_id'] is None: return None
                asset = c.execute('SELECT * FROM shadify_media_assets WHERE id=%s AND artist_id=%s AND track_id=%s',
                                  (row['asset_id'], row['artist_id'], track_id)).fetchone()
                if asset is None: return None
                return PlaybackTarget(track_id, row['publish_state'], row['access_class'], Asset(**asset), row['page_state'])

    def access_context(self,actor,artist):
        with self.authorized(actor,artist) as (_,auth,_,_): return auth.dto()

    def media_metadata(self,actor,asset_id):
        require_fresh_actor(actor)
        asset=self.get(asset_id)
        if asset is None: raise MediaError(404,'Asset not found')
        with self.authorized(actor,asset.artist_id,operation='read_content') as (c,auth,_,_):
            row=c.execute('SELECT * FROM shadify_media_assets WHERE id=%s AND artist_id=%s',(asset_id,asset.artist_id)).fetchone()
            if row is None: raise MediaError(404,'Asset not found')
            return {**auth.dto(),**{k:(str(row[k]) if k in ('artist_id','track_id') else row[k])
                for k in ('artist_id','track_id','asset_type','mime','size','state','access_class','publish_state')},'asset_id':str(asset_id),'outputs':self.ingest_outputs(c,asset_id),'ingest':self.ingest_status(c,asset_id) if row['state'] in ('uploaded','ready') else None}

    def project_user(self,account_id):
        # Admission wrapper owns provenance; this read cannot create or verify an identity.
        with self.authorized(account_id) as (c,auth,_,_):
            artists=c.execute('SELECT artist_id FROM artist_memberships WHERE user_id=%s AND revoked_at IS NULL ORDER BY artist_id',
                              (auth.user_id,)).fetchall()
            return {'user_id':str(auth.user_id),'account_id':auth.account_id,
                    'artist_ids':[str(r['artist_id']) for r in artists],
                    'app_admin':{'is_primary':auth.primary,'permissions':sorted(auth.permissions),
                                 'can_manage_admin':auth.primary}}

    def create_track(self,account_id,artist_id,title):
        result=self.content_write(account_id,artist_id,'tracks',{'title':title})
        content=result.pop('content')
        return {**result,'track_id':content['id'],'title':content['title'],'publish_state':content['publish_state']}

    def list_tracks(self,account_id,artist_id,limit=20,cursor=None):
        result=self.content_read(account_id,artist_id,'tracks',limit=limit,cursor=cursor)
        result['tracks']=result.pop('items')
        return result

    def ready(self):
        # Read-only compatibility check through009; never auto-migrate.
        with self.connect() as c:
            if c.execute('SELECT primary_user_id,revision FROM shadify_app_admin WHERE singleton=true').fetchone() is None:
                raise RuntimeError('Application admin state missing')
            c.execute('SELECT id,input_sha256,state,revision FROM shadify_ingest_jobs LIMIT 0')
            c.execute('SELECT job_id,verdict,engine_version,definitions_version,full_coverage FROM shadify_ingest_scans LIMIT 0')
            c.execute('SELECT asset_id,job_id,scan_id,object_identity FROM shadify_ingest_approvals LIMIT 0')
            c.execute('SELECT user_id,permissions FROM shadify_app_admin_delegates LIMIT 0')
            c.execute('SELECT actor_account_id,details FROM shadify_app_admin_audit LIMIT 0')
            c.execute('SELECT u.first_admitted_at,u.active,a.slug,a.publish_state,t.delivery_asset_id,m.storage_provider, '
                      'am.revoked_at,au.actor_account_id,r.data,v.data,mo.data,e.data '
                      'FROM shadify_users u,shadify_artists a,shadify_tracks t,shadify_media_assets m, '
                      'artist_memberships am,artist_ownership_audit au,shadify_releases r, '
                      'shadify_videos v,shadify_moments mo,shadify_events e LIMIT 0')
        return True
