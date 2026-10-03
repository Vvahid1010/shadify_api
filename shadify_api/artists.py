"""Artist/profile/content operations; one authority and stable artist scope."""
from uuid import UUID, uuid4
from datetime import timezone
from pydantic import ValidationError
from psycopg import IntegrityError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .artist_models import ArtistProfile, PublicUserProfile, RESOURCE_MODELS
from .media import MediaError

TABLES = {name: 'shadify_' + name for name in RESOURCE_MODELS}
REFS = {'tracks': ('original_asset_id','delivery_asset_id','preview_asset_id','artwork_asset_id'),
        'releases': ('cover_asset_id',), 'videos': ('track_id','delivery_asset_id'),
        'moments': ('track_id','image_asset_id'), 'events': ()}
AVAILABILITY = {'original_audio_upload': True, 'track_artwork_upload': True,
                'profile_media_upload': False, 'video_upload': False, 'processing': False,
                'playback_http': False, 'paid_entitlements': False, 'followers': False,
                'official_playlists': False, 'home_rails': False, 'commerce': False}


def validate(model, data):
    try: return model.model_validate(data)
    except ValidationError: raise MediaError(422, 'Invalid fields or references') from None


class ArtistOperations:
    def _page(self, artist):
        return {k: (str(v) if isinstance(v, UUID) else v) for k,v in artist.items()
                if k in ('id','slug','name','bio','genres','socials','publish_state','verified',
                         'avatar_asset_id','header_asset_id','layout')}

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
                      and row['storage_provider']=='r2' and row['key'].startswith(prefix)
                      and row['access_class'] in ('free','paid'))
            if kind in ('audio_preview','artwork_delivery'):
                eligible=eligible and row['access_class']=='free'
        if not eligible: raise MediaError(409,'Asset is not technically ready for this use')
        return row

    def _content_row(self,c,artist,resource,content_id):
        row=c.execute(f'SELECT * FROM {TABLES[resource]} WHERE id=%s AND artist_id=%s', (content_id,artist)).fetchone()
        if row is None: raise MediaError(404,'Content not found')
        return row

    def _content_dto(self,c,resource,row,*,public=False):
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
            data['track_ids']=[str(r['track_id']) for r in c.execute(
                'SELECT track_id FROM shadify_release_tracks WHERE release_id=%s ORDER BY position',(row['id'],)).fetchall()]
        if resource=='moments':
            data['official_artist_news']=True
            if not public: data['author_account_id']=row['author_account_id']
        return {'id':str(row['id']),'artist_id':str(row['artist_id']),'publish_state':row['publish_state'],**data}

    def create_artist(self,actor,body):
        artist_id=uuid4()
        if body.avatar_asset_id or body.header_asset_id:
            raise MediaError(409,'Create page before attaching same-page ready media')
        try:
            with self.authorized(actor,admin_only=True) as (c,auth,_,_):
                row=c.execute('INSERT INTO shadify_artists(id,slug,name,bio,genres,socials) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *',
                    (artist_id,body.slug,body.name,body.bio,Jsonb(body.genres),Jsonb(body.socials))).fetchone()
                from dataclasses import replace
                return {**replace(auth,artist_id=artist_id).dto(),'page':self._page(row),'availability':AVAILABILITY}
        except IntegrityError: raise MediaError(409,'Slug unavailable') from None

    def private_page(self,actor,artist):
        with self.authorized(actor,artist) as (c,auth,page,_):
            owner=c.execute('SELECT u.auth_account_id FROM artist_memberships m JOIN shadify_users u ON u.id=m.user_id '
                            'WHERE m.artist_id=%s AND m.revoked_at IS NULL',(artist,)).fetchone()
            return {**auth.dto(),'page':self._page(page),'owner_account_id':owner['auth_account_id'] if owner else None,
                    'availability':AVAILABILITY}

    def manageable_pages(self,actor,limit,cursor=None):
        with self.authorized(actor) as (c,auth,_,_):
            if auth.admin:
                rows=c.execute('SELECT * FROM shadify_artists WHERE (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s',
                               (cursor,cursor,limit+1)).fetchall()
            else:
                rows=c.execute('SELECT a.* FROM shadify_artists a JOIN artist_memberships m ON m.artist_id=a.id '
                               'WHERE m.user_id=%s AND m.revoked_at IS NULL AND (%s::uuid IS NULL OR a.id>%s) '
                               'ORDER BY a.id LIMIT %s',(auth.user_id,cursor,cursor,limit+1)).fetchall()
            from dataclasses import replace
            return {'actor_account_id':actor,'acting_as_admin':auth.admin,'capabilities':{'create_artist':auth.admin},
                'pages':[{'page':{k:self._page(row)[k] for k in ('id','name','slug','publish_state','verified')},
                **replace(auth,artist_id=row['id'],publish_state=row['publish_state']).dto()} for row in rows[:limit]],
                'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None,'availability':AVAILABILITY}

    def edit_page(self,actor,artist,patch):
        with self.authorized(actor,artist) as (c,auth,page,_):
            fields=ArtistProfile.model_fields
            model=validate(ArtistProfile,{**{k:page[k] for k in fields},**patch})
            for key in ('avatar_asset_id','header_asset_id'):
                self._asset(c,artist,getattr(model,key),'artwork_delivery')
            row=c.execute('UPDATE shadify_artists SET name=%s,bio=%s,genres=%s,socials=%s,avatar_asset_id=%s,header_asset_id=%s '
                          'WHERE id=%s RETURNING *',(model.name,model.bio,Jsonb(model.genres),Jsonb(model.socials),
                          model.avatar_asset_id,model.header_asset_id,artist)).fetchone()
            return {**auth.dto(),'page':self._page(row),'availability':AVAILABILITY}

    def edit_layout(self,actor,artist,layout):
        with self.authorized(actor,artist) as (c,auth,_,_):
            if layout.featured_work:
                self._content_row(c,artist,layout.featured_work.kind+'s',layout.featured_work.id)
            for resource,reference in (('videos',layout.featured_video_id),('moments',layout.pinned_moment_id)):
                if reference: self._content_row(c,artist,resource,reference)
            data=layout.model_dump(mode='json')
            c.execute('UPDATE shadify_artists SET layout=%s WHERE id=%s',(Jsonb(data),artist))
            return {**auth.dto(),'layout':data,'availability':AVAILABILITY}

    def page_state(self,actor,artist,state,*,admin_only=False,verification=None):
        with self.authorized(actor,artist,admin_only=admin_only) as (c,auth,page,_):
            if verification is not None:
                c.execute('UPDATE shadify_artists SET verified=%s WHERE id=%s',(verification,artist))
            else:
                if page['publish_state']=='suspended' and not admin_only:
                    raise MediaError(409,'Explicit admin suspension action required')
                if state=='published' and not page['name'].strip():
                    raise MediaError(409,'Page name required')
                c.execute('UPDATE shadify_artists SET publish_state=%s WHERE id=%s',(state,artist))
            page=c.execute('SELECT * FROM shadify_artists WHERE id=%s',(artist,)).fetchone()
            from dataclasses import replace
            return {**replace(auth,publish_state=page['publish_state']).dto(),'page':self._page(page)}

    def audit(self,actor,artist,limit,cursor=None):
        with self.authorized(actor,artist,admin_only=True) as (c,auth,_,_):
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
                        self._authority(c,actor,admin_only=True)
                        raise MediaError(404,'Profile not found')
                    auth,_,users=self._authority(c,actor,targets=(account['auth_account_id'],),admin_only=True)
                    row=users[account['auth_account_id']]
                    if patch is not None:
                        model=validate(PublicUserProfile,{**{k:row[k] for k in PublicUserProfile.model_fields},**patch})
                        row=c.execute('UPDATE shadify_users SET name=%s,slug=%s,bio=%s,genres=%s WHERE id=%s RETURNING *',
                                      (model.name,model.slug,model.bio,Jsonb(model.genres),user_id)).fetchone()
                    return {'actor_account_id':actor,'acting_as_admin':auth.admin,'profile':
                            {'user_id':str(row['id']),**{k:row[k] for k in PublicUserProfile.model_fields}}}
        except IntegrityError: raise MediaError(409,'Profile slug unavailable') from None

    def _validate_content(self,c,artist,resource,content_id,model):
        if resource in ('videos','moments') and model.track_id:
            self._content_row(c,artist,'tracks',model.track_id)
        if resource=='tracks':
            self._asset(c,artist,model.original_asset_id,'audio_original',track_id=content_id,original=True)
            self._asset(c,artist,model.delivery_asset_id,'audio_delivery',track_id=content_id)
            self._asset(c,artist,model.preview_asset_id,'audio_preview',track_id=content_id)
            self._asset(c,artist,model.artwork_asset_id,'artwork_delivery',track_id=content_id)
        elif resource=='releases':
            self._asset(c,artist,model.cover_asset_id,'artwork_delivery')
            for track in model.track_ids: self._content_row(c,artist,'tracks',track)
        elif resource=='videos':
            self._asset(c,artist,model.delivery_asset_id,'video_delivery',track_id=model.track_id)
        elif resource=='moments':
            self._asset(c,artist,model.image_asset_id,'artwork_delivery')

    def _write_content(self,c,actor,artist,resource,content_id,model,*,create):
        data=model.model_dump(mode='json')
        refs={k:getattr(model,k) for k in REFS[resource]}
        for key in refs: data.pop(key,None)
        track_ids=data.pop('track_ids',None) if resource=='releases' else None
        if resource=='events':
            data['starts_at']=model.starts_at.astimezone(timezone.utc).isoformat()
        extras={}
        if resource=='tracks':
            extras={'title':model.title,'access_class':model.access_class}
            data.pop('title'); data.pop('access_class')
        if resource=='moments' and create: extras={'author_account_id':actor}
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
        with self.authorized(actor,artist) as (c,auth,_,_):
            if create:
                model=validate(RESOURCE_MODELS[resource],payload)
            else:
                row=self._content_row(c,artist,resource,content_id)
                old=self._content_dto(c,resource,row)
                model=validate(RESOURCE_MODELS[resource],{**{k:old[k] for k in RESOURCE_MODELS[resource].model_fields if k in old},**payload})
            self._validate_content(c,artist,resource,content_id,model)
            row=self._write_content(c,actor,artist,resource,content_id,model,create=create)
            # Published works cannot be edited into a technically invalid state.
            if row['publish_state']=='published' and not self._publishable(c,artist,resource,row):
                raise MediaError(409,'Published content must remain technically ready')
            return {**auth.dto(),'content':self._content_dto(c,resource,row),'availability':AVAILABILITY}

    def content_read(self,actor,artist,resource,content_id=None,*,limit=20,cursor=None):
        with self.authorized(actor,artist) as (c,auth,_,_):
            if content_id:
                return {**auth.dto(),'content':self._content_dto(c,resource,self._content_row(c,artist,resource,content_id))}
            rows=c.execute(f'SELECT * FROM {TABLES[resource]} WHERE artist_id=%s AND (%s::uuid IS NULL OR id>%s) '
                           'ORDER BY id LIMIT %s',(artist,cursor,cursor,limit+1)).fetchall()
            return {**auth.dto(),'items':[self._content_dto(c,resource,r) for r in rows[:limit]],
                    'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None}

    def _publishable(self,c,artist,resource,row,*,lock=True):
        if resource=='tracks':
            if not row['title'].strip() or row['delivery_asset_id'] is None: return False
            try:
                asset=self._asset(c,artist,row['delivery_asset_id'],'audio_delivery',track_id=row['id'],lock=lock)
                return asset['access_class']==row['access_class']
            except MediaError: return False
        if resource=='videos':
            if row['delivery_asset_id'] is None: return False
            try:
                self._asset(c,artist,row['delivery_asset_id'],'video_delivery',track_id=row['track_id'],lock=lock)
                return True
            except MediaError: return False
        if resource=='releases':
            tracks=c.execute('SELECT t.* FROM shadify_release_tracks r JOIN shadify_tracks t ON t.id=r.track_id '
                             'AND t.artist_id=r.artist_id WHERE r.release_id=%s ORDER BY r.position',(row['id'],)).fetchall()
            return bool(tracks) and all(t['publish_state']=='published' and self._publishable(c,artist,'tracks',t,lock=lock) for t in tracks)
        return True

    def content_publication(self,actor,artist,resource,content_id,state):
        with self.authorized(actor,artist) as (c,auth,_,_):
            row=self._content_row(c,artist,resource,content_id)
            if state=='published' and (auth.publish_state=='suspended' or not self._publishable(c,artist,resource,row)):
                raise MediaError(409,'Page suspended or content not technically ready')
            row=c.execute(f'UPDATE {TABLES[resource]} SET publish_state=%s WHERE id=%s AND artist_id=%s RETURNING *',
                          (state,content_id,artist)).fetchone()
            return {**auth.dto(),'content':self._content_dto(c,resource,row)}

    def content_delete(self,actor,artist,resource,content_id):
        try:
            with self.authorized(actor,artist) as (c,auth,page,_):
                self._content_row(c,artist,resource,content_id)
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

    def public_page(self,*,artist_id=None,slug=None,limit=20,cursor=None):
        with self.connect() as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with connection.cursor(row_factory=dict_row) as c:
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
                # Resolve featured references independently of paginated lists.
                def visible(resource,reference):
                    if not reference: return False
                    row=c.execute(f"SELECT * FROM {TABLES[resource]} WHERE id=%s AND artist_id=%s AND publish_state='published'",
                                  (reference,artist)).fetchone()
                    return row is not None and self._publishable(c,artist,resource,row,lock=False)
                featured=layout.get('featured_work')
                if featured and not visible(featured['kind']+'s',featured['id']): layout['featured_work']=None
                for key,resource in (('featured_video_id','videos'),('pinned_moment_id','moments')):
                    if not visible(resource,layout.get(key)): layout[key]=None
                for key in ('avatar_asset_id','header_asset_id'):
                    try: self._asset(c,artist,page[key],'artwork_delivery',lock=False)
                    except MediaError: result[key]=None
                for entries in contents.values():
                    for entry in entries:
                        # Never emit a raw artist-scoped original through metadata.
                        entry.pop('original_asset_id',None)
                result['layout']=layout
                sections={'featured':bool(layout.get('featured_work')), 'songs':bool(contents['tracks']),
                          'video':bool(contents['videos']), 'moments':bool(contents['moments']),
                          'bio':bool(page['bio']), 'tour':bool(contents['events'])}
                result['sections']=[{**s,'available':True,'empty':not sections[s['id']]} for s in layout.get('sections',[])]
                return {'page':result,'content':contents,'next_cursors':cursors,'availability':AVAILABILITY}
