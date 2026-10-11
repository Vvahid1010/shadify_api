"""Isolated PostgreSQL + source ASGI editorial authority and immutable snapshot proof."""
from pathlib import Path
from uuid import UUID, uuid4
from unittest.mock import patch
import time
from fastapi import FastAPI
from fastapi.testclient import TestClient
import test_mood_discovery as fixtures
from test_artist_postgres import ArtistPostgresCase
from authority_fixture import admitted_actor
from shadify_api.repository import PostgresMediaRepository
from shadify_api.editorial_discovery import EditorialDiscovery, DraftWrite, Revision, EditorRevision
from shadify_api.editorial_discovery_routes import install_editorial_discovery_routes
from shadify_api.media import MediaError


class EditorialDiscoveryTests(ArtistPostgresCase):
    track=fixtures.MoodDiscoveryTests.track
    asset=fixtures.MoodDiscoveryTests.asset
    attach=fixtures.MoodDiscoveryTests.attach

    def setUp(self):
        super().setUp()
        self.apply('007_admin_nomination.sql','008_nomination_control.sql','009_ingest_quarantine.sql')
        with self.connect() as c:
            c.execute((Path(__file__).resolve().parents[1]/'schema_proposals/017_discover_editorial.sql').read_text())
            c.execute("UPDATE shadify_artists SET publish_state='published' WHERE id=%s",(self.artist,))
        self.repo=PostgresMediaRepository(self.connect)
        self.assign()
        self.editorial=EditorialDiscovery(self.repo)
        self.primary=admitted_actor('admin')
        self.editor_actor=admitted_actor('owner-b')
        self.first=self.track(['calm']);self.attach(self.first,self.asset(self.first))
        self.second=self.track();self.attach(self.second,self.asset(self.second))

    def version(self):
        state=self.editorial.read(self.primary)
        return Revision(expected_revision=state['revision'],expected_authority_revision=state['authority_revision'])

    def grant(self):
        state=self.editorial.read(self.primary)
        return self.editorial.editor(self.primary,'owner-b',EditorRevision(
            expected_editor_revision=state['editor_revision'],expected_authority_revision=state['authority_revision']),grant=True)

    def draft(self,ids,*,rail='calm',kind='playlist',title='Editorial pick',placement=None,entity=None):
        return {'rails':[{'id':rail,'entries':[{'id':str(placement or uuid4()),'kind':kind,
            'title':title,'entity_id':str(entity) if entity else None,'track_ids':list(map(str,ids))}]}]}

    def save(self,draft,actor=None,version=None):
        body=DraftWrite(**(version or self.version()).model_dump(),draft=draft)
        return self.editorial.save(actor or self.primary,body)

    def test_only_primary_publishes_even_when_editor_has_all_app_capabilities(self):
        self.grant()
        state=self.save(self.draft([self.first,self.second]),self.editor_actor)
        submitted=self.editorial.submit(self.editor_actor,self.version())
        self.assertEqual(submitted['status'],'pending')
        self.assertFalse(submitted['can_publish'])
        self.assertNotIn('editors',submitted)
        with self.connect() as c:
            c.execute("INSERT INTO shadify_app_admin_delegates(user_id,permissions) SELECT id,%s FROM shadify_users WHERE auth_account_id='owner-b'",
                (sorted(['create_artist','edit_profile','manage_content','publish_content',
                    'change_owner','suspend','verify','edit_user_profile']),))
        with self.assertRaises(MediaError) as denied:self.editorial.publish(self.editor_actor,self.version())
        self.assertEqual(denied.exception.status,403)
        self.assertTrue(all(not rail['entries'] for rail in self.editorial.public()['rails']))
        published=self.editorial.publish(self.primary,self.version())
        self.assertEqual(published['status'],'published')
        self.assertEqual(self.editorial.public()['rails'][0]['entries'][0]['track_ids'],list(map(str,[self.first,self.second])))

    def test_draft_edits_and_stale_approval_never_change_published_snapshot(self):
        self.grant()
        self.save(self.draft([self.first]))
        self.editorial.publish(self.primary,self.version())
        before=self.editorial.public()
        stale=self.version()
        self.save(self.draft([self.second]),self.editor_actor)
        self.assertEqual(self.editorial.public(),before)
        with self.assertRaises(MediaError) as conflict:self.editorial.publish(self.primary,stale)
        self.assertEqual(conflict.exception.status,409)
        self.assertEqual(self.editorial.public(),before)
        self.editorial.submit(self.editor_actor,self.version())
        reviewed=self.version()
        self.save(self.draft([self.first,self.second]),self.editor_actor)
        self.assertEqual(self.editorial.read(self.primary)['status'],'draft')
        with self.assertRaises(MediaError):self.editorial.publish(self.primary,reviewed)

    def test_live_eligibility_removes_unpublished_track_without_mutating_stored_snapshot(self):
        self.save(self.draft([self.first,self.second]))
        self.editorial.publish(self.primary,self.version())
        with self.connect() as c:
            original=c.execute('SELECT published FROM shadify_discover_state').fetchone()[0]
            c.execute("UPDATE shadify_tracks SET publish_state='draft' WHERE id=%s",(self.first,))
        public=self.editorial.public()
        self.assertEqual(public['rails'][0]['entries'][0]['track_ids'],[str(self.second)])
        self.assertNotIn(str(self.first),str(public))
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT published FROM shadify_discover_state').fetchone()[0],original)
            c.execute("UPDATE shadify_artists SET publish_state='suspended' WHERE id=%s",(self.artist,))
        self.assertTrue(all(not rail['entries'] for rail in self.editorial.public()['rails']))

    def test_selection_order_unlabeled_tracks_and_remove_without_delete(self):
        self.save(self.draft([self.second,self.first]))
        self.editorial.publish(self.primary,self.version())
        ids=self.editorial.public()['rails'][0]['entries'][0]['track_ids']
        self.assertEqual(ids,list(map(str,[self.second,self.first])))
        self.save({'rails':[]})
        self.editorial.publish(self.primary,self.version())
        self.assertTrue(all(not rail['entries'] for rail in self.editorial.public()['rails']))
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_tracks WHERE id=ANY(%s) AND publish_state=%s',
                ([self.first,self.second],'published')).fetchone()[0],2)

    def test_editor_delegation_is_primary_only_revocable_and_separate_from_artist_roles(self):
        for actor in (admitted_actor('listener'),admitted_actor('owner-a'),self.editor_actor):
            with self.assertRaises(MediaError):self.editorial.read(actor)
        state=self.grant()
        self.assertTrue(self.editorial.read(self.editor_actor)['can_edit'])
        with self.assertRaises(MediaError):
            self.editorial.editor(self.editor_actor,'listener',EditorRevision(
                expected_editor_revision=state['editor_revision'],expected_authority_revision=state['authority_revision']),grant=True)
        self.editorial.editor(self.primary,'owner-b',EditorRevision(
            expected_editor_revision=state['editor_revision'],expected_authority_revision=state['authority_revision']),grant=False)
        with self.assertRaises(MediaError):self.editorial.save(self.editor_actor,
            DraftWrite(**self.version().model_dump(),draft=self.draft([self.first])))
        self.grant()
        with self.connect() as c:
            c.execute("UPDATE shadify_users SET active=false WHERE auth_account_id='owner-b'")
        state=self.editorial.read(self.primary)
        self.editorial.editor(self.primary,'owner-b',EditorRevision(
            expected_editor_revision=state['editor_revision'],expected_authority_revision=state['authority_revision']),grant=False)

    def test_primary_transfer_authority_revision_and_expired_identity_fail_closed(self):
        self.save(self.draft([self.first]))
        stale=self.version()
        self.primary_fixture('listener',delegates='retain')
        with self.assertRaises(MediaError):self.editorial.publish(self.primary,stale)
        with self.assertRaises(MediaError) as conflict:
            self.editorial.publish(admitted_actor('listener'),stale)
        self.assertEqual(conflict.exception.status,409)
        now=int(time.time())
        expired=admitted_actor('listener',issued=now-2,expires=now-1)
        with self.assertRaises(MediaError):self.editorial.read(expired)

    def test_snapshot_freezes_release_membership_and_new_mood_tracks_do_not_autoappear(self):
        release=self.repo.content_write(self.primary,self.artist,'releases',
            {'title':'Release','track_ids':[str(self.first)]})['content']
        with self.connect() as c:
            c.execute("UPDATE shadify_releases SET publish_state='published' WHERE id=%s",(UUID(release['id']),))
        draft=self.draft([],rail='releases',kind='release',title='',entity=release['id'])
        self.save(draft)
        with self.connect() as c:
            c.execute('INSERT INTO shadify_release_tracks(release_id,artist_id,track_id,position) VALUES(%s,%s,%s,1)',
                (UUID(release['id']),self.artist,self.second))
        self.editorial.publish(self.primary,self.version())
        with self.assertRaises(MediaError):
            self.editorial.publish(self.primary,self.version())
        public=self.editorial.public()
        rail=next(rail for rail in public['rails'] if rail['id']=='releases')
        self.assertEqual(rail['entries'][0]['track_ids'],[str(self.first)])
        self.assertTrue(all(not rail['entries'] for rail in public['rails'] if rail['id'] in ('calm','romantic','happy','party')))

    def test_submit_publication_reject_invalid_selection_and_audit_is_append_only(self):
        self.save(self.draft([self.first]))
        with self.connect() as c:c.execute("UPDATE shadify_tracks SET publish_state='draft' WHERE id=%s",(self.first,))
        with self.assertRaises(MediaError):self.editorial.submit(self.primary,self.version())
        with self.assertRaises(MediaError):self.editorial.publish(self.primary,self.version())
        with self.connect() as c:
            actions=[row[0] for row in c.execute("SELECT action FROM shadify_app_admin_audit WHERE action LIKE 'discover_%'")]
            self.assertEqual(actions,['discover_save_draft'])
            with self.assertRaises(Exception):
                c.execute("UPDATE shadify_app_admin_audit SET details='{}' WHERE action='discover_save_draft'")


    def test_delegated_grant_denial_does_not_reveal_account_existence(self):
        self.grant()
        state=self.editorial.read(self.primary)
        body=EditorRevision(expected_editor_revision=state['editor_revision'],
            expected_authority_revision=state['authority_revision'])
        for target in ('listener','nonexistent'):
            with self.assertRaises(MediaError) as error:
                self.editorial.editor(self.editor_actor,target,body,grant=True)
            self.assertEqual(error.exception.status,403)

    def test_expiry_during_publish_audit_rolls_back_snapshot_and_audit(self):
        self.save(self.draft([self.first]))
        version=self.version()
        now=int(time.time());clock=[now]
        actor=admitted_actor('admin',issued=now,expires=now+1)
        audit=self.repo._admin_audit
        def expire(*args,**kwargs):
            result=audit(*args,**kwargs)
            clock[0]=actor.expires_at
            return result
        with patch('shadify_api.auth.time.time',side_effect=lambda:clock[0]), patch.object(self.repo,'_admin_audit',side_effect=expire):
            with self.assertRaises(MediaError) as error:self.editorial.publish(actor,version)
            self.assertEqual(error.exception.status,401)
        self.assertTrue(all(not rail['entries'] for rail in self.editorial.public()['rails']))
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM shadify_app_admin_audit WHERE action='discover_publish'").fetchone()[0],0)

    def test_installer_does_not_trust_client_primary_and_default_public_response_is_empty(self):
        from fastapi.responses import JSONResponse
        app=FastAPI()
        install_editorial_discovery_routes(app,lambda:admitted_actor('listener'),self.repo)
        @app.exception_handler(MediaError)
        def error(_,failure):return JSONResponse(status_code=failure.status,content={'detail':failure.detail})
        client=TestClient(app)
        self.assertEqual(client.get('/api/discover/editorial').status_code,200)
        self.assertTrue(all(not rail['entries'] for rail in client.get('/api/discover/editorial').json()['rails']))
        self.assertEqual(client.get('/api/admin/discover').status_code,403)
        self.assertEqual(client.post('/api/admin/discover/publish',json={
            **self.version().model_dump(),'is_primary':True}).status_code,422)
