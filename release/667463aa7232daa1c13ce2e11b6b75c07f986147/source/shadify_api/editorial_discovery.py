"""Purpose-built Discover drafts and primary-only publication. No runtime enrollment."""
from contextlib import contextmanager
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .artist_models import Input, MOOD_IDS
from .auth import require_fresh_actor
from .admin import active_identity
from .media import Asset, MediaError
from .delivery import PlaybackTarget, require_playable

RAILS = {
    'calm': ('Playlists for every mood · Calm', 'playlist'),
    'romantic': ('Playlists for every mood · Romantic', 'playlist'),
    'happy': ('Playlists for every mood · Happy', 'playlist'),
    'party': ('Playlists for every mood · Party', 'playlist'),
    'releases': ('Artist releases', 'release'),
    'tracks': ('Songs', 'track'),
    'videos': ('Music videos', 'video'),
    'video-playlists': ('Video playlists', None),
    'podcasts': ('Podcasts', None),
    'dj-sets': ('DJ sets', None),
    'artists': ('Artist spotlight', 'artist'),
}
MAX_REVISION = 9223372036854775807


from .discover_layout import LayoutEntry as Entry, LayoutSnapshot as Snapshot, TEMPLATES, registry


class Revision(Input):
    expected_revision: int = Field(strict=True, ge=0, le=MAX_REVISION)
    expected_authority_revision: int = Field(strict=True, ge=0, le=MAX_REVISION)


class DraftWrite(Revision):
    draft: Snapshot


class EditorRevision(Input):
    expected_editor_revision: int = Field(strict=True, ge=0, le=MAX_REVISION)
    expected_authority_revision: int = Field(strict=True, ge=0, le=MAX_REVISION)


class EditorialDiscovery:
    def __init__(self, repository):
        self.repository = repository

    @contextmanager
    def _access(self, actor, *, primary=False, targets=(), lock_sources=False):
        repo = self.repository
        with repo.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                require_fresh_actor(actor)
                hint_revision = None
                if lock_sources:
                    # Match the existing artist -> app-admin -> identity -> content
                    # lock order before validating source eligibility for publication.
                    hint = c.execute('SELECT draft,draft_revision FROM shadify_discover_state WHERE singleton=true').fetchone()
                    if hint is None:
                        raise MediaError(503,'Discover persistence unavailable')
                    hinted = Snapshot.model_validate(hint['draft'])
                    refs = {'artist':[], 'track':[], 'release':[], 'video':[]}
                    for rail in hinted.rails:
                        for entry in rail.entries:
                            refs['track'].extend(entry.track_ids)
                            if entry.entity_id is not None:
                                refs[entry.kind].append(entry.entity_id)
                    c.execute('SELECT id FROM shadify_artists WHERE id=ANY(%s) '
                        'OR id IN(SELECT artist_id FROM shadify_tracks WHERE id=ANY(%s)) '
                        'OR id IN(SELECT artist_id FROM shadify_releases WHERE id=ANY(%s)) '
                        'OR id IN(SELECT artist_id FROM shadify_videos WHERE id=ANY(%s)) '
                        'ORDER BY id FOR UPDATE',
                        (refs['artist'],refs['track'],refs['release'],refs['video'])).fetchall()
                    hint_revision = hint['draft_revision']
                auth, _, users = repo._authority(c, actor, None, targets=targets, admin_only=primary)
                if primary:
                    auth, users = repo._primary_actor(c,actor,repo._admin_state(c),targets)
                # App-admin state and admitted identities are already locked by the
                # existing authorizer. Discover-only delegation never grants artist rights.
                state = c.execute('SELECT * FROM shadify_discover_state WHERE singleton=true FOR UPDATE').fetchone()
                if state is None:
                    raise MediaError(503,'Discover persistence unavailable')
                if hint_revision is not None and hint_revision != state['draft_revision']:
                    raise MediaError(409,'Discover draft changed; review again')
                editor = c.execute('SELECT 1 FROM shadify_discover_editors WHERE user_id=%s',(auth.user_id,)).fetchone()
                if (primary and not auth.primary) or (not auth.primary and editor is None):
                    raise MediaError(403,'Discover management access unavailable')
                authority_revision = repo._admin_state(c)['revision']
                yield c, auth, state, users, authority_revision
                require_fresh_actor(actor)

    def _revision(self, state, authority_revision, body):
        if body.expected_authority_revision != authority_revision:
            raise MediaError(409,'Application administration changed; review again')
        if isinstance(body, Revision) and body.expected_revision != state['draft_revision']:
            raise MediaError(409,'Discover draft changed; refresh and review again')
        if isinstance(body, EditorRevision) and body.expected_editor_revision != state['editor_revision']:
            raise MediaError(409,'Discover editors changed; refresh and review again')

    def _state(self, c):
        return c.execute('SELECT * FROM shadify_discover_state WHERE singleton=true').fetchone()

    def _track(self, c, track_id, *, lock=False):
        row = c.execute("SELECT t.*,a.name AS artist_name,a.avatar_asset_id AS avatar_asset_id FROM shadify_tracks t JOIN shadify_artists a ON a.id=t.artist_id "
            "WHERE t.id=%s AND t.publish_state='published' AND a.publish_state='published'"+
            (' FOR SHARE OF t,a' if lock else ''),(track_id,)).fetchone()
        if row is None or not self.repository._publishable(c,row['artist_id'],'tracks',row,lock=lock):
            return None
        preview = row['access_class']=='paid'
        asset_id = row['preview_asset_id'] if preview else row['delivery_asset_id']
        try:
            asset = self.repository._asset(c,row['artist_id'],asset_id,
                'audio_preview' if preview else 'audio_delivery',track_id=track_id,lock=lock)
            if asset is None:
                return None
            require_playable(PlaybackTarget(track_id,'published',row['access_class'],Asset(**asset),'published'),
                track_id,preview=preview)
        except MediaError as error:
            if error.status in (403,404,409):
                return None
            raise
        return {'id':str(track_id),'artist_id':str(row['artist_id']),'artist_name':row['artist_name'],
            'title':row['title'],'publish_state':'published','access_class':row['access_class'],
            'avatar_asset_id':str(row['avatar_asset_id']) if row['avatar_asset_id'] else None,
            'mood_ids':[mood for mood in MOOD_IDS if mood in row['data'].get('mood_ids',[])],
            'preview_asset_id' if preview else 'delivery_asset_id':str(asset_id)}

    def _entry(self,c,entry,*,lock=False):
        if entry.kind=='playlist':
            tracks = [track for track_id in entry.track_ids if (track:=self._track(c,track_id,lock=lock)) is not None]
            return {'id':str(entry.id),'kind':'playlist','title':entry.title,'tracks':tracks,
                'track_ids':[track['id'] for track in tracks],
                'eligible':bool(tracks) and len(tracks)==len(entry.track_ids)} if tracks else None
        resource = {'track':'tracks','release':'releases','video':'videos','artist':'artists'}[entry.kind]
        if entry.kind=='track':
            track = self._track(c,entry.entity_id,lock=lock)
            return {'id':str(entry.id),'entity_id':str(entry.entity_id),'kind':'track',
                'title':entry.title or track['title'],'tracks':[track],'track_ids':[track['id']],
                'eligible':True} if track else None
        row = c.execute('SELECT r.* FROM shadify_'+resource+" r "+
            ("JOIN shadify_artists a ON a.id=r.artist_id " if resource!='artists' else '')+
            "WHERE r.id=%s AND r.publish_state='published' "+
            ("AND a.publish_state='published'" if resource!='artists' else '')+
            ((' FOR SHARE OF r,a' if resource!='artists' else ' FOR SHARE OF r') if lock else ''),
            (entry.entity_id,)).fetchone()
        if row is None:
            return None
        if entry.kind=='artist':
            return {'id':str(entry.id),'entity_id':str(entry.entity_id),'kind':'artist',
                'artist_id':str(entry.entity_id),'avatar_asset_id':str(row['avatar_asset_id']) if row['avatar_asset_id'] else None,
                'title':entry.title or row['name'],'tracks':[],'track_ids':[],'eligible':True}
        if not self.repository._publishable(c,row['artist_id'],resource,row,lock=lock):
            return None
        title = entry.title or row['data'].get('title','')
        if entry.kind=='video':
            return {'id':str(entry.id),'entity_id':str(entry.entity_id),'kind':'video','title':title,
                'tracks':[],'track_ids':[],'eligible':True,'artist_id':str(row['artist_id'])}
        selected = entry.track_ids or [r['track_id'] for r in c.execute(
            'SELECT track_id FROM shadify_release_tracks WHERE release_id=%s ORDER BY position',(entry.entity_id,)).fetchall()]
        tracks = [track for track_id in selected if (track:=self._track(c,track_id,lock=lock)) is not None]
        tracks = [track for track in tracks if track['artist_id']==str(row['artist_id'])]
        return {'id':str(entry.id),'entity_id':str(entry.entity_id),'kind':'release','title':title,
            'tracks':tracks,'track_ids':[track['id'] for track in tracks],
            'release_type':row['data'].get('kind'),
            'eligible':bool(tracks) and len(tracks)==len(selected)} if tracks else None

    def _resolve(self,c,snapshot,*,strict=False,public=False):
        result=[]
        for section in snapshot.sections:
            if public and not section.enabled:continue
            entries=[]
            for entry in section.entries:
                resolved=self._entry(c,entry,lock=strict and section.enabled)
                if strict and section.enabled and (resolved is None or not resolved['eligible']):
                    raise MediaError(409,'Selected Discover content is no longer eligible')
                if resolved is not None:
                    # Withdrawal removes unavailable tracks without rewriting history.
                    if public:resolved['eligible']=True
                    entries.append(resolved)
            row=section.model_dump(mode='json');row['entries']=entries
            row['available']=TEMPLATES[section.template_id][3] is None
            row['unavailable_reason']=TEMPLATES[section.template_id][3]
            result.append(row)
        return result

    def _dto(self,c,auth,state,authority_revision):
        result = {'actor_account_id':auth.account_id,'is_primary':auth.primary,'can_edit':True,
            'can_publish':auth.primary,'authority_revision':authority_revision,
            'revision':state['draft_revision'],'status':state['draft_status'],
            'published_revision':state['published_revision'],'editor_revision':state['editor_revision'],
            'draft':Snapshot.model_validate(state['draft']).model_dump(mode='json'),
            'published':Snapshot.model_validate(state['published']).model_dump(mode='json'),
            'registry_version':2,'templates':registry(),
            'preview':self._resolve(c,Snapshot.model_validate(state['draft'])),
            'published_preview':self._resolve(c,Snapshot.model_validate(state['published']))}
        if auth.primary:
            result['editors'] = [dict(row) for row in c.execute(
                'SELECT u.auth_account_id,u.name,u.active FROM shadify_discover_editors e '
                'JOIN shadify_users u ON u.id=e.user_id ORDER BY u.auth_account_id').fetchall()]
        return result

    def read(self,actor):
        with self._access(actor) as (c,auth,state,_,revision):
            return self._dto(c,auth,state,revision)

    def save(self,actor,body:DraftWrite):
        with self._access(actor) as (c,auth,state,_,revision):
            self._revision(state,revision,body)
            prepared=body.draft.model_copy(deep=True)
            # A release is selected as an explicit ordered membership snapshot at
            # preparation time, not as a live query that can add future songs.
            previous={entry.id:entry for rail in Snapshot.model_validate(state['draft']).rails for entry in rail.entries}
            for rail in prepared.rails:
                for entry in rail.entries:
                    if entry.kind=='release':
                        prior=previous.get(entry.id)
                        retained=(prior is not None and prior.kind=='release' and prior.entity_id==entry.entity_id
                                  and bool(entry.track_ids) and prior.track_ids==entry.track_ids)
                        if not retained:
                            canonical=[row['track_id'] for row in c.execute(
                                'SELECT track_id FROM shadify_release_tracks WHERE release_id=%s ORDER BY position',
                                (entry.entity_id,)).fetchall()]
                            if entry.track_ids and entry.track_ids!=canonical:
                                raise MediaError(409,'Release membership changed; select and review the current release')
                            entry.track_ids=canonical
                    if not entry.title:
                        resolved=self._entry(c,entry)
                        if resolved is None or not resolved['eligible']:
                            raise MediaError(409,'Selected Discover content is no longer eligible')
                        if not entry.title:
                            entry.title=resolved['title']
                        if entry.kind=='release' and not entry.track_ids:
                            entry.track_ids=[UUID(value) for value in resolved['track_ids']]
            prepared=Snapshot.model_validate(prepared.model_dump(mode='json'))
            after=prepared.model_dump(mode='json')
            c.execute("UPDATE shadify_discover_state SET draft=%s,draft_revision=draft_revision+1,"
                "draft_status='draft',submitted_revision=NULL WHERE singleton=true",
                (Jsonb(after),))
            self.repository._admin_audit(c,auth,'discover_save_draft',
                details={'before_revision':state['draft_revision'],'after_revision':state['draft_revision']+1,
                    'before':state['draft'],'after':after})
            return self._dto(c,auth,self._state(c),revision)

    def submit(self,actor,body:Revision):
        with self._access(actor,lock_sources=True) as (c,auth,state,_,revision):
            self._revision(state,revision,body)
            if state['draft_status']=='published':
                raise MediaError(409,'Save a new draft version before submitting')
            self._resolve(c,Snapshot.model_validate(state['draft']),strict=True)
            c.execute("UPDATE shadify_discover_state SET draft_status='pending',submitted_revision=draft_revision WHERE singleton=true")
            self.repository._admin_audit(c,auth,'discover_submit',details={'revision':state['draft_revision']})
            return self._dto(c,auth,self._state(c),revision)

    def publish(self,actor,body:Revision):
        with self._access(actor,primary=True,lock_sources=True) as (c,auth,state,_,revision):
            self._revision(state,revision,body)
            if state['draft_status']=='published':
                raise MediaError(409,'This Discover version is already published')
            snapshot = Snapshot.model_validate(state['draft'])
            resolved = self._resolve(c,snapshot,strict=True)
            lookup = {entry['id']:entry for rail in resolved for entry in rail['entries']}
            # Freeze card titles and release membership at explicit primary approval.
            for rail in snapshot.rails:
                if not rail.enabled:continue
                for entry in rail.entries:
                    entry.title = lookup[str(entry.id)]['title']
                    if entry.kind=='release':
                        entry.track_ids = [UUID(value) for value in lookup[str(entry.id)]['track_ids']]
            published = snapshot.model_dump(mode='json')
            c.execute("UPDATE shadify_discover_state SET published=%s,published_revision=draft_revision,"
                "published_by=%s,published_at=clock_timestamp(),draft_status='published',submitted_revision=NULL "
                "WHERE singleton=true",(Jsonb(published),auth.user_id))
            self.repository._admin_audit(c,auth,'discover_publish',details={
                'revision':state['draft_revision'],'previous_published_revision':state['published_revision'],
                'snapshot':published})
            return self._dto(c,auth,self._state(c),revision)

    def editor(self,actor,account_id,body:EditorRevision,*,grant):
        if not 1 <= len(account_id)<=36:
            raise MediaError(422,'Existing account ID required')
        with self._access(actor,primary=True,targets=(account_id,) if grant else ()) as (c,auth,state,users,revision):
            self._revision(state,revision,body)
            target = users.get(account_id) if grant else c.execute(
                'SELECT * FROM shadify_users WHERE auth_account_id=%s',(account_id,)).fetchone()
            if target is None or (grant and not active_identity(target)) or target['id']==auth.user_id:
                raise MediaError(422,'Existing active non-primary account required')
            if grant:
                c.execute('INSERT INTO shadify_discover_editors(user_id,granted_by) VALUES(%s,%s) '
                    'ON CONFLICT(user_id) DO UPDATE SET granted_by=EXCLUDED.granted_by,granted_at=clock_timestamp()',
                    (target['id'],auth.user_id))
            else:
                c.execute('DELETE FROM shadify_discover_editors WHERE user_id=%s',(target['id'],))
            c.execute('UPDATE shadify_discover_state SET editor_revision=editor_revision+1 WHERE singleton=true')
            self.repository._admin_audit(c,auth,'discover_grant_editor' if grant else 'discover_revoke_editor',
                target=account_id,details={'scope':'discover_editor','can_publish':False,
                    'before_revision':state['editor_revision'],'after_revision':state['editor_revision']+1})
            return self._dto(c,auth,self._state(c),revision)

    def public(self):
        with self.repository.connect() as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            with connection.cursor(row_factory=dict_row) as c:
                state = self._state(c)
                if state is None:
                    raise MediaError(503,'Discover persistence unavailable')
                return {'revision':state['published_revision'],
                    'schema_version':2,'registry_version':2,
                    'settings':Snapshot.model_validate(state['published']).settings.model_dump(),
                    'sections':self._resolve(c,Snapshot.model_validate(state['published']),public=True),
                    'rails':self._resolve(c,Snapshot.model_validate(state['published']),public=True)}

    def candidates(self,actor,kind,*,q='',mood=None,cursor=None):
        if kind not in ('track','release','video','artist','playlist') or mood is not None and mood not in MOOD_IDS:
            raise MediaError(422,'Invalid Discover candidates request')
        with self._access(actor) as (c,auth,state,_,revision):
            if kind=='playlist':
                items={}
                for key in ('draft','published'):
                    for section in Snapshot.model_validate(state[key]).sections:
                        for entry in section.entries:
                            if entry.kind=='playlist' and q.casefold() in entry.title.casefold():
                                item=self._entry(c,entry)
                                if item and item['eligible']:items[str(entry.id)]=item
                values=[items[key] for key in sorted(items) if cursor is None or key>str(cursor)]
                return {'items':values[:20],'next_cursor':values[19]['id'] if len(values)>20 else None,'authority_revision':revision,'actor_account_id':auth.account_id}
            table = {'track':'tracks','release':'releases','video':'videos','artist':'artists'}[kind]
            title = 'r.name' if kind=='artist' else 'r.title' if kind=='track' else "r.data->>'title'"
            rows = c.execute('SELECT r.id FROM shadify_'+table+" r WHERE r.publish_state='published' "
                'AND (%s::uuid IS NULL OR r.id>%s) AND '+title+" ILIKE %s "+
                ("AND (%s::text IS NULL OR r.data->'mood_ids' ? %s) " if kind=='track' else '')+
                'ORDER BY r.id LIMIT 101',
                (cursor,cursor,'%'+q[:120]+'%',*((mood,mood) if kind=='track' else ()))).fetchall()
            result=[];last=None;inspected=0
            for row in rows[:100]:
                last=row['id'];inspected+=1
                entry=Entry(id=row['id'],kind=kind,entity_id=row['id'])
                item=self._entry(c,entry)
                if item is not None and item['eligible']:
                    result.append(item)
                if len(result)==20:break
            return {'items':result,'next_cursor':str(last) if last is not None and inspected<len(rows) else None,
                'authority_revision':revision,'actor_account_id':auth.account_id}
