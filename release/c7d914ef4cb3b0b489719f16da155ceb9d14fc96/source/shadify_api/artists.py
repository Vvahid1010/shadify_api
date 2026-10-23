"""Artist/profile/content operations; one authority and stable artist scope."""
from uuid import UUID, uuid4
from datetime import timezone
from pydantic import ValidationError
from psycopg import IntegrityError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .artist_models import ArtistProfile, PublicUserProfile, RESOURCE_MODELS
from .media import Asset,MediaError
from .auth import require_fresh_actor
from . import track_media
from .releases import ReleaseOperations
from .artist_identity import ArtistIdentityOperations
import re

TABLES = {name: 'shadify_' + name for name in RESOURCE_MODELS}
REFS = {'tracks': ('original_asset_id','delivery_asset_id','preview_asset_id','artwork_asset_id'),
        'releases': ('cover_asset_id',), 'videos': ('track_id','delivery_asset_id'),
        'moments': ('track_id','image_asset_id'), 'events': ()}
AVAILABILITY = {'original_audio_upload': True, 'track_artwork_upload': True,
                'profile_media_upload': False, 'video_upload': False, 'processing': False,
                'playback_http': True, 'guest_playback_ingress': True, 'paid_entitlements': False, 'followers': False,
                'official_playlists': False, 'home_rails': False, 'commerce': False}


def validate(model, data):
    try: return model.model_validate(data)
    except ValidationError: raise MediaError(422, 'Invalid fields or references') from None


class ArtistOperations(ArtistIdentityOperations, ReleaseOperations):
    def _availability(self,c):
        return {**AVAILABILITY,'profile_media_upload':self.profile_media_available(c),'artist_identity':self.identity_available(c)}

    def _profile_asset(self,c,artist,asset_id,purpose,*,lock=True,anchor=True):
        if asset_id is None:return None
        row=c.execute('SELECT * FROM shadify_media_assets WHERE id=%s AND artist_id=%s'+(' FOR SHARE' if lock else ''),(asset_id,artist)).fetchone()
        if row is None:raise MediaError(404,'Asset not found')
        # Existing artwork references remain valid; new profile images are purpose-bound.
        if row['asset_type']=='artwork_delivery':
            return self._asset(c,artist,asset_id,'artwork_delivery',lock=lock)
        if (row['asset_type']!=purpose+'_delivery' or row['track_id'] is not None
                or row['state']!='ready' or row['access_class']!='free'
                or row['publish_state']!='published' or row['storage_provider']!='local'
                or not row['key'].startswith(f'delivery/{asset_id}/')):
            raise MediaError(409,'Approved same-purpose profile image required')
        self.require_ingest_approval(Asset(**row),cursor=c,lock=lock)
        if anchor and (row['mime']!='image/png' or c.execute(
                "SELECT 1 FROM shadify_ingest_variants v JOIN shadify_ingest_jobs j ON j.id=v.job_id "
                "WHERE v.asset_id=%s AND v.role='cover' AND j.state='ready'",(asset_id,)).fetchone() is None):
            raise MediaError(409,'Approved compatible profile image anchor required')
        return row

    def _page(self, artist):
        return {k: (str(v) if isinstance(v, UUID) else v) for k,v in artist.items()
                if k in ('id','slug','name','bio','genres','socials','publish_state','verified',
                         'avatar_asset_id','header_asset_id','layout','primary_language','localized','genre_ids','slug_changed_at')}

    def _asset(self, c, artist_id, asset_id, kind, *, track_id=None, original=False, lock=True):
        if asset_id is None: return None
        row=c.execute('SELECT * FROM shadify_media_assets WHERE id=%s AND artist_id=%s' + (' FOR SHARE' if lock else ''), (asset_id,artist_id)).fetchone()
        if row is None or (track_id is not None and row['track_id']!=track_id):
            raise MediaError(404,'Asset not found')
        if row['asset_type']!=kind:
            raise MediaError(422,'Asset kind mismatch')
        if original:
            eligible=(row['state'] in ('uploaded','ready') and row['access_class']=='private' and row['publish_state']=='draft')
        else:
            prefix={'audio_delivery':'delivery/','audio_preview':'previews/',
                    'video_delivery':'delivery/','artwork_delivery':'delivery/'}[kind]
            eligible=(row['state']=='ready' and row['publish_state']=='published'
                      and row['storage_provider'] in ('local','r2') and (row['key'].startswith(prefix) or row['key'].startswith(f"artists/{artist_id}/tracks/{row['track_id']}/{row['id']}/") or (kind=='artwork_delivery' and row['key'].startswith(f"covers/{row['id']}/")))
                      and row['access_class'] in ('free','paid'))
            if kind in ('audio_preview','artwork_delivery'):
                eligible=eligible and row['access_class']=='free'
        if not eligible: raise MediaError(409,'Asset is not technically ready for this use')
        if not original:self.require_ingest_approval(Asset(**row),cursor=c,lock=lock)
        return row

    def _track_artwork(self,c,artist,track,asset_id):
        """Draft originals stay private; delivery references keep their ready gate."""
        if asset_id is None: return None
        row=c.execute('SELECT asset_type FROM shadify_media_assets WHERE id=%s AND artist_id=%s '
                      'AND track_id=%s FOR SHARE',(asset_id,artist,track)).fetchone()
        if row is None: raise MediaError(404,'Asset not found')
        if row['asset_type']=='artwork_original':
            return self._asset(c,artist,asset_id,'artwork_original',track_id=track,original=True)
        return self._asset(c,artist,asset_id,'artwork_delivery',track_id=track)

    def _content_row(self,c,artist,resource,content_id):
        row=c.execute(f'SELECT * FROM {TABLES[resource]} WHERE id=%s AND artist_id=%s', (content_id,artist)).fetchone()
        if row is None: raise MediaError(404,'Content not found')
        return row

    def _content_dto(self,c,resource,row,*,public=False,memberships=None):
        data=dict(row['data'])
        if resource=='tracks': data.update(title=row['title'],access_class=row['access_class'])
        for key in REFS[resource]:
            if public and key=='original_asset_id': continue
            data[key]=str(row[key]) if row[key] is not None else None
        if public:
            for key in REFS[resource]:
                if key=='original_asset_id': continue
                reference=row[key]
                if key=='track_id' and reference is not None:
                    linked=c.execute("SELECT * FROM shadify_tracks WHERE id=%s AND artist_id=%s AND publish_state='published'",
                                     (reference,row['artist_id'])).fetchone()
                    if linked is None or not self._publishable(c,row['artist_id'],'tracks',linked,lock=False):
                        data[key]=None
                elif key.endswith('asset_id') and reference is not None:
                    kinds={'preview_asset_id':'audio_preview','artwork_asset_id':'artwork_delivery',
                           'cover_asset_id':'artwork_delivery','image_asset_id':'artwork_delivery',
                           'delivery_asset_id':'video_delivery' if resource=='videos' else 'audio_delivery'}
                    try:
                        self._asset(c,row['artist_id'],reference,kinds[key],
                                    track_id=row['id'] if resource=='tracks' else None,lock=False)
                    except MediaError: data[key]=None
        if resource=='releases':
            if not public: data['revision']=row['revision']
            data['track_ids']=[str(r['track_id']) for r in c.execute(
                'SELECT track_id FROM shadify_release_tracks WHERE release_id=%s ORDER BY position',(row['id'],)).fetchall()]
        if resource=='moments':
            data['official_artist_news']=True
            if not public: data['author_account_id']=row['author_account_id']
        if resource=='tracks' and not public:
            data['media_readiness']=track_media.readiness(self,c,row)
            data['independently_published']=row['independently_published']
            memberships=memberships if memberships is not None else self._track_memberships(c,row['artist_id'],[row['id']])
            data['release_memberships']=memberships.get(str(row['id']),[])
        return {'id':str(row['id']),'artist_id':str(row['artist_id']),'publish_state':row['publish_state'],**data}

    def create_artist(self,actor,body):
        artist_id=uuid4()
        if body.avatar_asset_id or body.header_asset_id:
            raise MediaError(409,'Create page before attaching same-page ready media')
        try:
            with self.authorized(actor,admin_only=True,operation='create_artist',audit=False) as (c,auth,_,_):
                row=c.execute('INSERT INTO shadify_artists(id,slug,name,bio,genres,socials) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *',
                    (artist_id,body.slug,body.name,body.bio,Jsonb(body.genres),Jsonb(body.socials))).fetchone()
                if self.identity_available(c):
                    entries=body.localized or [dict(language=body.primary_language,name=body.name,bio=body.bio,aliases=[])]
                    entries=[entry.model_dump() if hasattr(entry,'model_dump') else entry for entry in entries]
                    primary=next(entry for entry in entries if entry['language']==body.primary_language)
                    row=c.execute('UPDATE shadify_artists SET primary_language=%s,localized=%s,genre_ids=%s,name=%s,bio=%s WHERE id=%s RETURNING *',(body.primary_language,Jsonb(entries),Jsonb(body.genre_ids),primary['name'],primary['bio'],artist_id)).fetchone()
                self._admin_audit(c,auth,'create_artist',artist=artist_id)
                from dataclasses import replace
                return {**replace(auth,artist_id=artist_id).dto(),'page':self._page(row),'availability':self._availability(c)}
        except IntegrityError: raise MediaError(409,'Slug unavailable') from None

    def private_page(self,actor,artist):
        with self.authorized(actor,artist) as (c,auth,page,_):
            owner=c.execute('SELECT u.auth_account_id FROM artist_memberships m JOIN shadify_users u ON u.id=m.user_id '
                            'WHERE m.artist_id=%s AND m.revoked_at IS NULL',(artist,)).fetchone()
            return {**auth.dto(),'page':self._page(page),'owner_account_id':owner['auth_account_id'] if owner else None,
                    'availability':self._availability(c)}

    def manageable_pages(self,actor,limit,cursor=None):
        from dataclasses import replace
        from .admin import PAGE_PERMISSIONS
        with self.authorized(actor) as (c,auth,_,_):
            owned = {r['artist_id'] for r in c.execute('SELECT artist_id FROM artist_memberships '
                     'WHERE user_id=%s AND revoked_at IS NULL', (auth.user_id,)).fetchall()}
            page_admin = bool(auth.permissions & PAGE_PERMISSIONS)
            if page_admin:
                rows=c.execute('SELECT * FROM shadify_artists WHERE (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s',
                               (cursor,cursor,limit+1)).fetchall()
            else:
                rows=c.execute('SELECT a.* FROM shadify_artists a JOIN artist_memberships m ON m.artist_id=a.id '
                               'WHERE m.user_id=%s AND m.revoked_at IS NULL AND (%s::uuid IS NULL OR a.id>%s) '
                               'ORDER BY a.id LIMIT %s',(auth.user_id,cursor,cursor,limit+1)).fetchall()
            return {'actor_account_id':auth.account_id,'acting_as_admin':auth.admin,
                'capabilities':{'create_artist':'create_artist' in auth.permissions},
                'pages':[{'page':{k:self._page(row)[k] for k in ('id','name','slug','publish_state','verified')},
                **replace(auth,artist_id=row['id'],publish_state=row['publish_state'],
                          owner=row['id'] in owned,acting_admin=page_admin).dto()} for row in rows[:limit]],
                'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None,'availability':self._availability(c)}

    def edit_page(self,actor,artist,patch):
        with self.authorized(actor,artist,operation='edit_profile') as (c,auth,page,_):
            fields=ArtistProfile.model_fields
            model=validate(ArtistProfile,{**{k:page[k] for k in fields if k in page},**patch})
            model=self.identity_profile(c,page,patch,model)
            for key in ('avatar_asset_id','header_asset_id'):
                self._profile_asset(c,artist,getattr(model,key),key.removesuffix('_asset_id'))
            row=c.execute('UPDATE shadify_artists SET name=%s,bio=%s,genres=%s,socials=%s,avatar_asset_id=%s,header_asset_id=%s '
                          'WHERE id=%s RETURNING *',(model.name,model.bio,Jsonb(model.genres),Jsonb(model.socials),
                          model.avatar_asset_id,model.header_asset_id,artist)).fetchone()
            if self.identity_available(c):
                row=c.execute('UPDATE shadify_artists SET primary_language=%s,localized=%s,genre_ids=%s WHERE id=%s RETURNING *',(model.primary_language,Jsonb([entry.model_dump() for entry in model.localized]),Jsonb(model.genre_ids),artist)).fetchone()
            return {**auth.dto(),'page':self._page(row),'availability':self._availability(c)}

    def edit_layout(self,actor,artist,layout):
        with self.authorized(actor,artist,operation='edit_profile') as (c,auth,_,_):
            if layout.featured_work:
                self._content_row(c,artist,layout.featured_work.kind+'s',layout.featured_work.id)
            for resource,reference in (('videos',layout.featured_video_id),('moments',layout.pinned_moment_id)):
                if reference: self._content_row(c,artist,resource,reference)
            data=layout.model_dump(mode='json')
            c.execute('UPDATE shadify_artists SET layout=%s WHERE id=%s',(Jsonb(data),artist))
            return {**auth.dto(),'layout':data,'availability':self._availability(c)}

    def page_state(self,actor,artist,state,*,admin_only=False,verification=None):
        with self.authorized(actor,artist,admin_only=admin_only, operation='verify' if verification is not None else ('suspend' if admin_only else 'publish_content')) as (c,auth,page,_):
            if verification is not None:
                c.execute('UPDATE shadify_artists SET verified=%s WHERE id=%s',(verification,artist))
            else:
                if admin_only and state == 'published' and not auth.allows('publish_content'):
                    raise MediaError(403,'Publication permission required to resume published page')
                if page['publish_state']=='suspended' and not admin_only:
                    raise MediaError(409,'Explicit admin suspension action required')
                if state=='published' and not page['name'].strip():
                    raise MediaError(409,'Page name required')
                if state=='published':
                    for field in ('avatar_asset_id','header_asset_id'):
                        self._profile_asset(c,artist,page[field],field.removesuffix('_asset_id'))
                c.execute('UPDATE shadify_artists SET publish_state=%s WHERE id=%s',(state,artist))
            page=c.execute('SELECT * FROM shadify_artists WHERE id=%s',(artist,)).fetchone()
            from dataclasses import replace
            return {**replace(auth,publish_state=page['publish_state']).dto(),'page':self._page(page)}

    def audit(self,actor,artist,limit,cursor=None):
        with self.authorized(actor,artist,admin_only=True,operation='change_owner',audit=False) as (c,auth,_,_):
            rows=c.execute('SELECT * FROM artist_ownership_audit WHERE artist_id=%s AND (%s::uuid IS NULL OR id>%s) '
                           'ORDER BY id LIMIT %s',(artist,cursor,cursor,limit+1)).fetchall()
            return {**auth.dto(),'events':[{k:str(v) if isinstance(v,UUID) else v for k,v in r.items()} for r in rows[:limit]],
                    'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None}

    def public_user_profile(self,actor,user_id,patch=None):
        # Target is local user UUID; never inspect central Authentication data.
        try:
            with self.connect() as connection:
                with connection.cursor(row_factory=dict_row) as c:
                    account=c.execute('SELECT auth_account_id FROM shadify_users WHERE id=%s',(user_id,)).fetchone()
                    if account is None:
                        self._authority(c,actor,admin_only=True,operation='edit_user_profile')
                        raise MediaError(404,'Profile not found')
                    auth,_,users=self._authority(c,actor,targets=(account['auth_account_id'],),admin_only=True,operation='edit_user_profile')
                    row=users[account['auth_account_id']]
                    if patch is not None:
                        model=validate(PublicUserProfile,{**{k:row[k] for k in PublicUserProfile.model_fields},**patch})
                        row=c.execute('UPDATE shadify_users SET name=%s,slug=%s,bio=%s,genres=%s WHERE id=%s RETURNING *',
                                      (model.name,model.slug,model.bio,Jsonb(model.genres),user_id)).fetchone()
                    result={'actor_account_id':auth.account_id,'acting_as_admin':auth.admin,'profile':
                            {'user_id':str(row['id']),**{k:row[k] for k in PublicUserProfile.model_fields}}}
                    require_fresh_actor(actor)
                    if patch is not None:
                        self._admin_audit(c,auth,'edit_user_profile',target=account['auth_account_id'])
                    require_fresh_actor(actor)
                    return result
        except IntegrityError: raise MediaError(409,'Profile slug unavailable') from None

    def _validate_content(self,c,artist,resource,content_id,model):
        if resource in ('videos','moments') and model.track_id:
            self._content_row(c,artist,'tracks',model.track_id)
        if resource=='tracks':
            self._asset(c,artist,model.original_asset_id,'audio_original',track_id=content_id,original=True)
            self._asset(c,artist,model.delivery_asset_id,'audio_delivery',track_id=content_id)
            self._asset(c,artist,model.preview_asset_id,'audio_preview',track_id=content_id)
            self._track_artwork(c,artist,content_id,model.artwork_asset_id)
        elif resource=='releases':
            if model.kind=='Single' and len(model.track_ids)>1:
                raise MediaError(422,'A Single can contain only one song')
            self._asset(c,artist,model.cover_asset_id,'artwork_delivery')
            for track in model.track_ids: self._content_row(c,artist,'tracks',track)
        elif resource=='videos':
            self._asset(c,artist,model.delivery_asset_id,'video_delivery',track_id=model.track_id)
        elif resource=='moments':
            self._asset(c,artist,model.image_asset_id,'artwork_delivery')

    def _write_content(self,c,actor,artist,resource,content_id,model,*,create):
        data=model.model_dump(mode='json')
        if resource=='releases' and not create:
            # Publication time is written only by the guarded publication action.
            previous=c.execute('SELECT data FROM shadify_releases WHERE id=%s AND artist_id=%s',(content_id,artist)).fetchone()
            if previous and previous['data'].get('published_at'):data['published_at']=previous['data']['published_at']
        refs={k:getattr(model,k) for k in REFS[resource]}
        for key in refs: data.pop(key,None)
        track_ids=data.pop('track_ids',None) if resource=='releases' else None
        if resource=='events':
            data['starts_at']=model.starts_at.astimezone(timezone.utc).isoformat()
        extras={}
        if resource=='tracks':
            extras={'title':model.title,'access_class':model.access_class}
            data.pop('title'); data.pop('access_class')
        if resource=='moments' and create: extras={'author_account_id':actor.account_id}
        values={'data':Jsonb(data),**refs,**extras}
        table=TABLES[resource]
        if create:
            values={'id':content_id,'artist_id':artist,**values}
            columns=','.join(values)
            row=c.execute(f'INSERT INTO {table} ({columns}) VALUES ('+','.join(['%s']*len(values))+') RETURNING *',tuple(values.values())).fetchone()
        else:
            row=c.execute(f'UPDATE {table} SET '+','.join(k+'=%s' for k in values)+' WHERE id=%s AND artist_id=%s RETURNING *',
                          (*values.values(),content_id,artist)).fetchone()
        if resource=='releases':
            c.execute('DELETE FROM shadify_release_tracks WHERE release_id=%s',(content_id,))
            for position,track in enumerate(model.track_ids):
                c.execute('INSERT INTO shadify_release_tracks(release_id,artist_id,track_id,position) VALUES (%s,%s,%s,%s)',
                          (content_id,artist,track,position))
        return row

    def content_write(self,actor,artist,resource,payload,content_id=None):
        create=content_id is None
        content_id=content_id or uuid4()
        with self.authorized(actor,artist,operation='manage_content') as (c,auth,_,_):
            payload=dict(payload)
            revision=payload.pop('expected_revision',None) if resource=='releases' else None
            expected=payload.pop('expected_sources',None) if resource=='tracks' else None
            preparation=payload.pop('prepare_media',False) if resource=='tracks' else False
            if create:
                model=validate(RESOURCE_MODELS[resource],payload)
            else:
                row=self._content_row(c,artist,resource,content_id)
                if resource=='releases':
                    self._release_revision(row,revision)
                    previous=self._content_dto(c,resource,row)
                    if row['publish_state']=='published' and any(key in payload and payload[key]!=previous.get(key) for key in ('kind','cover_asset_id','track_ids')):
                        raise MediaError(409,'Unpublish the release before changing its songs, order, type or cover')
                if resource=='tracks':
                    row=c.execute('SELECT * FROM shadify_tracks WHERE id=%s AND artist_id=%s FOR UPDATE',
                                  (content_id,artist)).fetchone()
                    if preparation:
                        if preparation is not True or payload:
                            raise MediaError(422,'Preparation accepts only expected current inputs')
                        row=track_media.prepare(self,c,row,expected)
                        return {**auth.dto(),'content':self._content_dto(c,resource,row),'availability':self._availability(c)}
                    if expected is not None:
                        track_media.check_expected(c,row,expected)
                    for field,kind in (('original_asset_id','audio'),('artwork_asset_id','cover')):
                        if field in payload and str(payload[field])!=str(row[field]):
                            candidate=dict(row)
                            try:
                                candidate[field]=UUID(str(payload[field])) if payload[field] is not None else None
                            except (ValueError,TypeError,AttributeError):
                                raise MediaError(422,'Invalid input reference') from None
                            if track_media.current_source(c,candidate,'audio' if kind=='audio' else 'artwork') != track_media.current_source(c,row,'audio' if kind=='audio' else 'artwork'):
                                if row['publish_state']!='draft':
                                    raise MediaError(409,'Unpublish before replacing media')
                                if expected is None or kind not in expected:
                                    raise MediaError(409,'Expected current input required for replacement')
                                if kind=='audio':
                                    payload['delivery_asset_id']=None
                                    payload['preview_asset_id']=None
                old=self._content_dto(c,resource,row)
                model=validate(RESOURCE_MODELS[resource],{**{k:old[k] for k in RESOURCE_MODELS[resource].model_fields if k in old},**payload})
            self._validate_content(c,artist,resource,content_id,model)
            row=self._write_content(c,actor,artist,resource,content_id,model,create=create)
            if resource=='releases' and not create:
                row=c.execute('UPDATE shadify_releases SET revision=revision+1 WHERE id=%s AND artist_id=%s RETURNING *',(content_id,artist)).fetchone()
            # Published works cannot be edited into a technically invalid state.
            if row['publish_state']=='published' and not self._publishable(c,artist,resource,row):
                raise MediaError(409,'Published content must remain technically ready')
            if (not create and resource=='tracks' and row['publish_state']=='published'
                    and any(key in payload and str(payload[key])!=str(old.get(key)) for key in REFS['tracks'])
                    and not track_media.readiness(self,c,row,lock=True)['ready']):
                raise MediaError(409,'Published media must retain current input readiness')
            return {**auth.dto(),'content':self._content_dto(c,resource,row),'availability':self._availability(c)}

    def content_read(self,actor,artist,resource,content_id=None,*,limit=20,cursor=None):
        with self.authorized(actor,artist,operation='read_content') as (c,auth,_,_):
            if content_id:
                result={**auth.dto(),'content':self._content_dto(c,resource,self._content_row(c,artist,resource,content_id))}
                if resource=='releases':
                    tracks=self._release_tracks(c,artist,content_id)
                    memberships=self._track_memberships(c,artist,[t['id'] for t in tracks])
                    result['tracks']=[self._content_dto(c,'tracks',t,memberships=memberships) for t in tracks]
                return result
            rows=c.execute(f'SELECT * FROM {TABLES[resource]} WHERE artist_id=%s AND (%s::uuid IS NULL OR id>%s) '
                           'ORDER BY id LIMIT %s',(artist,cursor,cursor,limit+1)).fetchall()
            memberships=self._track_memberships(c,artist,[r['id'] for r in rows[:limit]]) if resource=='tracks' else None
            return {**auth.dto(),'items':[self._content_dto(c,resource,r,memberships=memberships) for r in rows[:limit]],
                    'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None}

    def _publishable(self,c,artist,resource,row,*,lock=True):
        if resource=='tracks':
            if not row['title'].strip() or row['delivery_asset_id'] is None: return False
            try:
                asset=self._asset(c,artist,row['delivery_asset_id'],'audio_delivery',track_id=row['id'],lock=lock)
                if row['artwork_asset_id'] is not None:
                    self._asset(c,artist,row['artwork_asset_id'],'artwork_delivery',
                                track_id=row['id'],lock=lock)
                return asset['access_class']==row['access_class']
            except MediaError: return False
        if resource=='videos':
            if row['delivery_asset_id'] is None: return False
            try:
                self._asset(c,artist,row['delivery_asset_id'],'video_delivery',track_id=row['track_id'],lock=lock)
                return True
            except MediaError: return False
        if resource=='releases':
            try:self._asset(c,artist,row['cover_asset_id'],'artwork_delivery',lock=lock)
            except MediaError:return False
            tracks=c.execute('SELECT t.* FROM shadify_release_tracks r JOIN shadify_tracks t ON t.id=r.track_id '
                             'AND t.artist_id=r.artist_id WHERE r.release_id=%s ORDER BY r.position',(row['id'],)).fetchall()
            return bool(tracks) and all(t['publish_state']=='published' and self._publishable(c,artist,'tracks',t,lock=lock) for t in tracks)
        if resource=='moments':
            try:self._asset(c,artist,row['image_asset_id'],'artwork_delivery',lock=lock)
            except MediaError:return False
        return True

    def content_publication(self,actor,artist,resource,content_id,state,*,expected_revision=None,publish_included_tracks=False):
        with self.authorized(actor,artist,operation='publish_content') as (c,auth,_,_):
            row=self._content_row(c,artist,resource,content_id)
            if resource=='releases':
                return self._release_publication(c,auth,artist,row,state,expected_revision,publish_included_tracks)
            if resource=='tracks':
                row=c.execute('SELECT * FROM shadify_tracks WHERE id=%s AND artist_id=%s FOR UPDATE',
                              (content_id,artist)).fetchone()
                if state=='draft' and self._published_release_dependency(c,artist,content_id):
                    raise MediaError(409,'Unpublish the releases containing this song first; it is still available through them')
                if state=='published' and not track_media.readiness(self,c,row,lock=True)['ready']:
                    raise MediaError(409,'Both current input scans and outputs must be ready and attached')
            if state=='published' and (auth.publish_state=='suspended' or not self._publishable(c,artist,resource,row)):
                raise MediaError(409,'Page suspended or content not technically ready')
            extra=',independently_published='+('true' if state=='published' else 'false') if resource=='tracks' else ''
            row=c.execute(f'UPDATE {TABLES[resource]} SET publish_state=%s'+extra+' WHERE id=%s AND artist_id=%s RETURNING *',
                          (state,content_id,artist)).fetchone()
            return {**auth.dto(),'content':self._content_dto(c,resource,row)}

    def content_delete(self,actor,artist,resource,content_id,*,expected_revision=None):
        try:
            with self.authorized(actor,artist,operation='manage_content') as (c,auth,page,_):
                row=self._content_row(c,artist,resource,content_id)
                if resource=='releases':
                    self._release_revision(row,expected_revision)
                    if row['publish_state']=='published':
                        raise MediaError(409,'Unpublish the release before deleting it')
                layout=page['layout']
                featured=layout.get('featured_work') or {}
                if ((resource in ('tracks','releases') and featured.get('kind','')+'s'==resource and featured.get('id')==str(content_id))
                    or (resource=='videos' and layout.get('featured_video_id')==str(content_id))
                    or (resource=='moments' and layout.get('pinned_moment_id')==str(content_id))):
                    raise MediaError(409,'Content referenced by layout')
                if resource=='releases':
                    c.execute('DELETE FROM shadify_release_tracks WHERE release_id=%s',(content_id,))
                c.execute(f'DELETE FROM {TABLES[resource]} WHERE id=%s AND artist_id=%s',(content_id,artist))
                return {**auth.dto(),'deleted_id':str(content_id)}
        except IntegrityError: raise MediaError(409,'Content still referenced; detach first') from None

    def published_artist_catalog(self,actor,limit=20,cursor=None,q=''):
        require_fresh_actor(actor)
        result=self.public_artist_catalog(limit,cursor,q)
        require_fresh_actor(actor)
        return result

    def public_artist_catalog(self,limit=20,cursor=None,q=''):
        # Public projection; deployed route retains canonical Authentication-peer admission.
        with self.connect() as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with connection.cursor(row_factory=dict_row) as c:
                if self.identity_available(c):
                    query=c.execute('SELECT normalize_artist_search(%s) AS query',(q,)).fetchone()['query'];tokens=re.findall(r'[^\W_]+',query,flags=re.UNICODE)
                    expression=' & '.join(token+':*' for token in tokens)
                    rows=c.execute("WITH matches AS (SELECT a.*,CASE WHEN %s=ANY(normalized_names) THEN 0 WHEN search_names@@to_tsquery('simple',%s) THEN 1 ELSE 2 END AS rank FROM shadify_artists a WHERE publish_state='published' AND (%s='' OR search_names@@to_tsquery('simple',%s) OR search_bio@@to_tsquery('simple',%s))) SELECT * FROM matches WHERE %s::uuid IS NULL OR (rank,id)>(COALESCE((SELECT rank FROM matches WHERE id=%s),2),%s) ORDER BY rank,id LIMIT %s",(query,expression,query,expression,expression,cursor,cursor,cursor,limit+1)).fetchall()
                else:
                    rows=c.execute("SELECT * FROM shadify_artists WHERE publish_state='published' AND (%s::uuid IS NULL OR id>%s) AND (%s='' OR strpos(lower(name || ' ' || genres::text),lower(%s))>0) ORDER BY id LIMIT %s",(cursor,cursor,q.strip(),q.strip(),limit+1)).fetchall()
                artists=[]
                for row in rows[:limit]:
                    page=self._page(row);page.pop('layout',None);page.pop('slug_changed_at',None)
                    for key in ('avatar_asset_id','header_asset_id'):
                        try:self._profile_asset(c,row['id'],row[key],key.removesuffix('_asset_id'),lock=False)
                        except MediaError:page[key]=None
                    artists.append(page)
                return {'artists':artists,'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None}

    def public_page(self,*,artist_id=None,slug=None,limit=20,cursor=None):
        with self.connect() as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with connection.cursor(row_factory=dict_row) as c:
                if slug and self.identity_available(c):
                    found=c.execute('SELECT artist_id FROM shadify_artist_slugs WHERE slug=%s',(slug,)).fetchone()
                    if found:artist_id=found['artist_id']
                page=c.execute("SELECT * FROM shadify_artists WHERE publish_state='published' AND "
                               "((%s::uuid IS NOT NULL AND id=%s) OR (%s::text IS NOT NULL AND slug=%s))",
                               (artist_id,artist_id,slug,slug)).fetchone()
                if page is None: raise MediaError(404,'Artist not found')
                artist=page['id']; result=self._page(page); layout=dict(page['layout'])
                result['curated_by_shadify']=c.execute('SELECT 1 FROM artist_memberships WHERE artist_id=%s AND revoked_at IS NULL',(artist,)).fetchone() is None
                contents={}; cursors={}
                for resource,table in TABLES.items():
                    # UUID pagination bounds the response and DB candidate scan.
                    rows=c.execute(f"SELECT * FROM {table} WHERE artist_id=%s AND publish_state='published' "
                                   "AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",(artist,cursor,cursor,limit+1)).fetchall()
                    contents[resource]=[self._content_dto(c,resource,row,public=True) for row in rows[:limit]
                                        if self._publishable(c,artist,resource,row,lock=False)]
                    cursors[resource]=str(rows[limit-1]['id']) if len(rows)>limit else None
                # Actual publication chronology is independent of UUID cursors.
                # Older releases keep unknown dates; never invent a backfill.
                latest_releases=[]
                # Bound candidate work, but apply the six-item limit to eligible
                # releases rather than allowing six revoked rows to hide them.
                for offset in range(0,120,6):
                    latest_rows=c.execute("SELECT * FROM shadify_releases WHERE artist_id=%s AND publish_state='published' "
                                          "ORDER BY data->>'published_at' DESC NULLS LAST,id LIMIT 6 OFFSET %s",(artist,offset)).fetchall()
                    for row in latest_rows:
                        if self._publishable(c,artist,'releases',row,lock=False):
                            latest_releases.append(self._content_dto(c,'releases',row,public=True))
                            if len(latest_releases)==6:break
                    if len(latest_releases)==6 or len(latest_rows)<6:break
                # Resolve featured references independently of paginated lists.
                featured_content=[]
                def visible(resource,reference):
                    if not reference: return False
                    row=c.execute(f"SELECT * FROM {TABLES[resource]} WHERE id=%s AND artist_id=%s AND publish_state='published'",
                                  (reference,artist)).fetchone()
                    if row is None or not self._publishable(c,artist,resource,row,lock=False): return False
                    featured_content.append({**self._content_dto(c,resource,row,public=True),'resource':resource})
                    return True
                featured=layout.get('featured_work')
                if featured and not visible(featured['kind']+'s',featured['id']): layout['featured_work']=None
                for key,resource in (('featured_video_id','videos'),('pinned_moment_id','moments')):
                    if not visible(resource,layout.get(key)): layout[key]=None
                for key in ('avatar_asset_id','header_asset_id'):
                    try: self._profile_asset(c,artist,page[key],key.removesuffix('_asset_id'),lock=False)
                    except MediaError: result[key]=None
                for entries in contents.values():
                    for entry in entries:
                        # Never emit a raw artist-scoped original through metadata.
                        entry.pop('original_asset_id',None)
                result.pop('slug_changed_at',None)
                result['canonical_path']='/artist/'+page['slug']
                result['layout']=layout
                sections={'featured':bool(layout.get('featured_work')), 'songs':bool(contents['tracks']),
                          'video':bool(contents['videos']), 'moments':bool(contents['moments']),
                          'bio':bool(page['bio']), 'tour':bool(contents['events'])}
                result['sections']=[{**s,'available':True,'empty':not sections[s['id']]} for s in layout.get('sections',[])]
                return {'page':result,'content':contents,'featured_content':featured_content,'latest_releases':latest_releases,'next_cursors':cursors,'availability':self._availability(c)}
