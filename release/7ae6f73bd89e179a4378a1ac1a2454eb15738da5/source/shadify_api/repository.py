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
from .profile_media import profile_asset, profile_operation, PROFILE_ORIGINALS


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

    def require_profile_upload(self, actor, artist):
        with self.authorized(actor, artist, operation='edit_profile', audit=False) as (c,_,_,_):
            if not self.profile_media_available(c):
                raise MediaError(503,'Profile image schema integration unavailable')

    def profile_media_available(self,c):
        row=c.execute("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid='shadify_media_assets'::regclass AND conname='shadify_profile_scope') AS enabled").fetchone()
        return bool(row and row['enabled'])

    def insert(self,actor,asset):
        with self.authorized(actor,asset.artist_id,operation=profile_operation(asset)) as (c,_,_,_):
            if asset.owner_id != actor.account_id:
                raise MediaError(422,'Uploader audit identity mismatch')
            if profile_asset(asset):
                if asset.asset_type not in PROFILE_ORIGINALS or asset.track_id is not None or not self.profile_media_available(c):
                    raise MediaError(422,'Artist profile image scope mismatch')
            elif c.execute('SELECT 1 FROM shadify_tracks WHERE id=%s AND artist_id=%s',(asset.track_id,asset.artist_id)).fetchone() is None:
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
        with self.authorized(actor,asset.artist_id,operation=profile_operation(asset),audit=False) as (c,_,_,_):
            row=c.execute('SELECT * FROM shadify_media_assets WHERE id=%s AND artist_id=%s',(asset_id,asset.artist_id)).fetchone()
            return Asset(**row) if row else None

    def transition(self,actor,asset_id,expected,target,*,byte_sha256=None):
        require_fresh_actor(actor)
        if (expected,target) not in {('upload_pending','uploaded'),('upload_pending','failed')}:
            raise ValueError('Only original upload completion transitions are supported')
        asset=self.get(asset_id)
        if asset is None: raise MediaError(404,'Asset not found')
        with self.authorized(actor,asset.artist_id,operation=profile_operation(asset)) as (c,_,_,_):
            changed=c.execute('UPDATE shadify_media_assets SET state=%s WHERE id=%s AND artist_id=%s AND state=%s '
                "AND expires_at>clock_timestamp() AND asset_type IN ('audio_original','artwork_original','video_original','avatar_original','header_original')",
                (target,asset_id,asset.artist_id,expected)).rowcount==1
            if changed and target=='uploaded':self.enqueue_ingest(c,asset_id,byte_sha256)
            return changed

    @contextmanager
    def upload_guard(self, actor, asset):
        with self.authorized(actor, asset.artist_id, operation=profile_operation(asset)) as (c, _, _, _):
            row = c.execute('SELECT * FROM shadify_media_assets WHERE id=%s FOR UPDATE', (asset.id,)).fetchone()
            if (row is None or Asset(**row) != asset or row['state'] != 'upload_pending'
                    or row['expires_at'] <= datetime.now(timezone.utc)):
                raise MediaError(409, 'Upload state changed or expired')
            if profile_asset(asset):
                if asset.asset_type not in PROFILE_ORIGINALS or asset.track_id is not None or not self.profile_media_available(c):
                    raise MediaError(422,'Artist profile image scope mismatch')
            elif c.execute('SELECT 1 FROM shadify_tracks WHERE id=%s AND artist_id=%s',
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

    def public_profile_asset(self,artist,purpose,*,expected_asset=None,format='png',size='full'):
        if purpose not in ('avatar','header') or format not in ('png','avif') or size not in ('full','small'):raise MediaError(404,'Profile image not found')
        with self.connect() as connection,connection.cursor(row_factory=dict_row) as c:
            page=c.execute('SELECT avatar_asset_id,header_asset_id FROM shadify_artists WHERE id=%s AND publish_state=\'published\'',(artist,)).fetchone()
            asset_id=page[purpose+'_asset_id'] if page else None
            if asset_id is None:raise MediaError(404,'Profile image not found')
            row=self._profile_asset(c,artist,asset_id,purpose,lock=False)
            if row['asset_type']!=purpose+'_delivery':raise MediaError(404,'Profile image not found')
            selected=asset_id
            if format=='avif':
                quality=c.execute("SELECT source_quality FROM shadify_ingest_variants WHERE asset_id=%s AND role='cover'",(asset_id,)).fetchone()
                # The current sealed AVIF encoder has no alpha stream. Deliver
                # lossless compatible PNG when decoded source evidence has alpha.
                if quality and quality['source_quality'].get('has_alpha') is True:format='png'
            if expected_asset is not None and expected_asset!=asset_id or format!='png' or size!='full':
                role='cover'+('_avif' if format=='avif' else '')+('_small' if size=='small' else '')
                variant=c.execute("SELECT v.asset_id FROM shadify_ingest_variants anchor JOIN shadify_ingest_variants v ON v.job_id=anchor.job_id JOIN shadify_ingest_jobs j ON j.id=v.job_id WHERE anchor.asset_id=%s AND anchor.role='cover' AND j.state='ready' AND "+('v.asset_id=%s' if expected_asset is not None else 'v.role=%s'),(asset_id,expected_asset if expected_asset is not None else role)).fetchone()
                if variant is None:
                    if expected_asset is not None:raise MediaError(404,'Profile image not found')
                else:selected=variant['asset_id']
            selected_row=self._profile_asset(c,artist,selected,purpose,lock=False,anchor=False)
            return Asset(**selected_row)

    def require_public_profile_asset(self,asset):
        if asset.asset_type not in ('avatar_delivery','header_delivery') or asset.track_id is not None:
            raise MediaError(404,'Profile image not found')
        current=self.public_profile_asset(asset.artist_id,asset.asset_type.removesuffix('_delivery'),expected_asset=asset.id)
        if current!=asset:raise MediaError(409,'Profile image changed')

    def public_cover_asset(self,resource,content_id,*,expected_asset=None):
        # Only named published works. Never accept an asset/key from the browser.
        from .track_media import readiness
        if resource not in ('tracks','releases'):raise MediaError(404,'Cover not found')
        table='shadify_'+resource
        with self.connect() as connection,connection.cursor(row_factory=dict_row) as c:
            row=c.execute('SELECT work.* FROM '+table+" work JOIN shadify_artists artist ON artist.id=work.artist_id WHERE work.id=%s AND work.publish_state='published' AND artist.publish_state='published'",(content_id,)).fetchone()
            if row is None or not self._publishable(c,row['artist_id'],resource,row,lock=False):raise MediaError(404,'Cover not found')
            tracks=[row] if resource=='tracks' else c.execute('SELECT t.* FROM shadify_release_tracks r JOIN shadify_tracks t ON t.id=r.track_id AND t.artist_id=r.artist_id WHERE r.release_id=%s ORDER BY r.position',(content_id,)).fetchall()
            if not tracks or any(not readiness(self,c,track)['ready'] for track in tracks):raise MediaError(404,'Cover not found')
            asset_id=row['artwork_asset_id' if resource=='tracks' else 'cover_asset_id']
            if asset_id is None or expected_asset is not None and asset_id!=expected_asset:raise MediaError(404,'Cover not found')
            asset=Asset(**self._asset(c,row['artist_id'],asset_id,'artwork_delivery',track_id=row['id'] if resource=='tracks' else None,lock=False))
            # Local bytes provide current authority rechecks. No R2 bearer URL
            # or ordinary artwork AVIF is admitted by this capability.
            if asset.mime!='image/png' or asset.storage_provider!='local':raise MediaError(404,'Cover not found')
            return asset

    def require_public_cover_asset(self,resource,content_id,asset):
        current=self.public_cover_asset(resource,content_id,expected_asset=asset.id)
        if current!=asset:raise MediaError(409,'Cover changed')

    def media_metadata(self,actor,asset_id):
        require_fresh_actor(actor)
        asset=self.get(asset_id)
        if asset is None: raise MediaError(404,'Asset not found')
        with self.authorized(actor,asset.artist_id,operation='edit_profile' if profile_asset(asset) else 'read_content') as (c,auth,_,_):
            row=c.execute('SELECT * FROM shadify_media_assets WHERE id=%s AND artist_id=%s',(asset_id,asset.artist_id)).fetchone()
            if row is None: raise MediaError(404,'Asset not found')
            return {**auth.dto(),**{k:(str(row[k]) if k in ('artist_id','track_id') and row[k] is not None else row[k])
                for k in ('artist_id','track_id','asset_type','mime','size','state','access_class','publish_state')},'asset_id':str(asset_id),'outputs':self.ingest_outputs(c,asset_id),'ingest':self.ingest_status(c,asset_id) if row['state'] in ('uploaded','ready') else None,**({'profile_checks':self.profile_checks(c,asset)} if profile_asset(asset) else {})}

    def profile_checks(self,c,asset):
        job=c.execute("SELECT j.* FROM shadify_ingest_jobs j WHERE j.asset_id=%s OR EXISTS(SELECT 1 FROM shadify_ingest_variants v WHERE v.job_id=j.id AND v.asset_id=%s)",(asset.id,asset.id)).fetchone()
        if job is None:return {'scan':'pending','optimization':'pending'}
        scan=c.execute("SELECT verdict,full_coverage FROM shadify_ingest_scans WHERE job_id=%s AND input_sha256=%s ORDER BY scanned_at DESC,id DESC LIMIT 1",(job['id'],job['input_sha256'])).fetchone()
        scanned='passed' if scan and scan['verdict']=='clean' and scan['full_coverage'] else 'rejected' if scan and scan['verdict']=='infected' else 'running' if job['state']=='scanning' else 'pending'
        optimized='rejected' if job['state']=='rejected' else 'running' if job['state'] in ('validating','matching','processing') else 'pending'
        if job['last_error']=='bytes_changed':scanned='unavailable'
        if job['state']=='ready' and scanned=='passed':
            variants=c.execute("SELECT v.role,a.* FROM shadify_ingest_variants v JOIN shadify_media_assets a ON a.id=v.asset_id WHERE v.job_id=%s",(job['id'],)).fetchall()
            try:
                for variant in variants:self.require_ingest_approval(Asset(**{key:variant[key] for key in Asset.__dataclass_fields__}),cursor=c)
                roles={variant['role'] for variant in variants}
                optimized='passed' if {'master','cover'}<=roles else 'unavailable'
            except MediaError:optimized='unavailable'
        return {'scan':scanned,'optimization':optimized}

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
        return {**result,'content':content,'track_id':content['id'],'title':content['title'],'publish_state':content['publish_state']}

    def list_tracks(self,account_id,artist_id,limit=20,cursor=None):
        result=self.content_read(account_id,artist_id,'tracks',limit=limit,cursor=cursor)
        result['tracks']=result.pop('items')
        return result

    def ready(self):
        # Read-only compatibility check including020; never auto-migrate.
        with self.connect() as c:
            if c.execute('SELECT primary_user_id,revision FROM shadify_app_admin WHERE singleton=true').fetchone() is None:
                raise RuntimeError('Application admin state missing')
            c.execute('SELECT id,input_sha256,state,revision FROM shadify_ingest_jobs LIMIT 0')
            c.execute('SELECT job_id,verdict,engine_version,definitions_version,full_coverage FROM shadify_ingest_scans LIMIT 0')
            c.execute('SELECT asset_id,job_id,scan_id,object_identity FROM shadify_ingest_approvals LIMIT 0')
            c.execute('SELECT revision FROM shadify_releases LIMIT 0')
            c.execute('SELECT independently_published FROM shadify_tracks LIMIT 0')
            c.execute('SELECT user_id,permissions FROM shadify_app_admin_delegates LIMIT 0')
            c.execute('SELECT actor_account_id,details FROM shadify_app_admin_audit LIMIT 0')
            c.execute('SELECT u.first_admitted_at,u.active,a.slug,a.publish_state,t.delivery_asset_id,m.storage_provider, '
                      'am.revoked_at,au.actor_account_id,r.data,v.data,mo.data,e.data '
                      'FROM shadify_users u,shadify_artists a,shadify_tracks t,shadify_media_assets m, '
                      'artist_memberships am,artist_ownership_audit au,shadify_releases r, '
                      'shadify_videos v,shadify_moments mo,shadify_events e LIMIT 0')
        return True
