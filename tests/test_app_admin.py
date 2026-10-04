from pathlib import Path
import unittest
import time
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
import psycopg
from fastapi.testclient import TestClient
from shadify_api.main import create_app
from shadify_api.domain import DomainService
from shadify_api.artist_models import ArtistCreate
from shadify_api.admin import PERMISSIONS
from shadify_api.auth import require_fresh_actor
from authority_fixture import admitted_actor
from shadify_api.repository import PostgresMediaRepository
from shadify_api.media import MediaError
from test_artist_postgres import ArtistPostgresCase


class AppAdminTests(ArtistPostgresCase):
    def setUp(self):
        super().setUp()
        self.repo = PostgresMediaRepository(self.connect)

    def test_verification_only_proof(self):
        primary = admitted_actor('admin')
        delegate = admitted_actor('listener')
        self.repo.set_delegate(primary, 'listener', ['verify'])
        result = self.repo.page_state(delegate, self.artist, None, admin_only=True, verification=True)
        self.assertTrue(result['page']['verified'])
        self.assertFalse(result['capabilities']['change_owner'])
        with self.assertRaises(MediaError) as error:
            self.repo.change_owner(delegate, self.artist, action='assign', target='owner-a')
        self.assertEqual(error.exception.status, 403)
        self.repo.set_delegate(primary, 'listener', [])
        with self.assertRaises(MediaError) as error:
            self.repo.page_state(delegate, self.artist, None, admin_only=True, verification=False)
        self.assertEqual(error.exception.status, 403)
        with self.connect() as c:
            self.assertTrue(c.execute('SELECT verified FROM shadify_artists WHERE id=%s', (self.artist,)).fetchone()[0])
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_app_admin_audit').fetchone()[0], 4)

    def test_delegates_cannot_redelegate_promote_or_replace_primary(self):
        primary, delegate = admitted_actor('admin'), admitted_actor('listener')
        # Even every operation capability cannot confer authority-management rights.
        self.repo.set_delegate(primary, 'listener', sorted(PERMISSIONS))
        for action in (
            lambda:self.repo.set_delegate(delegate,'owner-a',['verify']),
            lambda:self.repo.set_delegate(delegate,'listener',sorted(PERMISSIONS)),
            lambda:self.repo.transfer_primary(delegate,'owner-a',expected_primary='admin',delegates='retain'),
            lambda:self.repo.admin_details(delegate),
            lambda:self.repo.admin_audit(delegate)):
            with self.assertRaises(MediaError) as error: action()
            self.assertEqual(error.exception.status,403)

    def test_transfer_requires_explicit_policy_and_preserves_owner_independence(self):
        primary=admitted_actor('admin')
        self.assign(target='admin')
        self.repo.set_delegate(primary,'listener',['verify'])
        for policy in (None,'',True,'default'):
            with self.assertRaises(MediaError) as error:
                self.repo.transfer_primary(primary,'owner-b',expected_primary='admin',delegates=policy)
            self.assertEqual(error.exception.status,422)
        self.repo.transfer_primary(primary,'owner-b',expected_primary='admin',delegates='retain')
        with self.repo.authorized(primary,self.artist,operation='edit_profile') as (_,auth,_,_):
            self.assertFalse(auth.admin)
            self.assertTrue(auth.owner)
        with self.assertRaises(MediaError): self.repo.set_delegate(primary,'listener',[])
        self.repo.page_state(admitted_actor('listener'),self.artist,None,admin_only=True,verification=True)
        self.repo.transfer_primary(admitted_actor('owner-b'),'owner-a',expected_primary='owner-b',delegates='revoke')
        with self.assertRaises(MediaError):
            self.repo.page_state(admitted_actor('listener'),self.artist,None,admin_only=True,verification=False)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_app_admin_delegates').fetchone()[0],0)
            primary_account=c.execute('SELECT u.auth_account_id FROM shadify_app_admin a JOIN shadify_users u ON u.id=a.primary_user_id').fetchone()[0]
            self.assertEqual(primary_account,'owner-a')

    def test_unknown_unproven_and_inactive_targets_no_identity_guessing(self):
        primary=admitted_actor('admin')
        with self.connect() as c:
            c.execute("INSERT INTO shadify_users(id,auth_account_id) VALUES (%s,'unproven')",(uuid4(),))
            c.execute("UPDATE shadify_users SET active=false WHERE auth_account_id='owner-b'")
        for target in ('unknown','unproven','owner-b','somebody@example.invalid'):
            for action in (lambda:self.repo.set_delegate(primary,target,['verify']),
                           lambda:self.repo.transfer_primary(primary,target,expected_primary='admin',delegates='retain')):
                with self.assertRaises(MediaError) as error: action()
                self.assertEqual(error.exception.status,422)
        for permissions in (['verify','verify'],['admin'],['finance'],[None],'verify'):
            with self.assertRaises(MediaError):self.repo.set_delegate(primary,'listener',permissions)
        with self.assertRaises(MediaError):self.repo.set_delegate(primary,'admin',['verify'])
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_app_admin_delegates').fetchone()[0],0)

    def test_inactive_delegate_can_be_revoked_and_cannot_self_reactivate(self):
        self.repo.set_delegate(admitted_actor('admin'),'listener',['verify'])
        with self.connect() as c:c.execute("UPDATE shadify_users SET active=false WHERE auth_account_id='listener'")
        self.repo.observe_identity(admitted_actor('listener'))
        with self.assertRaises(MediaError):self.repo.page_state(admitted_actor('listener'),self.artist,None,admin_only=True,verification=True)
        self.repo.set_delegate(admitted_actor('admin'),'listener',[])
        with self.connect() as c:
            self.assertFalse(c.execute("SELECT active FROM shadify_users WHERE auth_account_id='listener'").fetchone()[0])
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_app_admin_delegates').fetchone()[0],0)

    def test_audit_failure_rolls_back_primary_and_delegate_changes(self):
        primary=admitted_actor('admin')
        self.repo.set_delegate(primary,'listener',['verify'])
        with self.connect() as c:
            c.execute("CREATE FUNCTION reject_admin_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic audit failure'; END $$")
            c.execute('CREATE TRIGGER reject_admin_audit BEFORE INSERT ON shadify_app_admin_audit FOR EACH ROW EXECUTE FUNCTION reject_admin_audit()')
        for action in (lambda:self.repo.set_delegate(primary,'listener',[]),
                       lambda:self.repo.transfer_primary(primary,'owner-b',expected_primary='admin',delegates='revoke'),
                       lambda:self.repo.page_state(admitted_actor('listener'),self.artist,None,admin_only=True,verification=True)):
            with self.assertRaises(psycopg.Error):action()
        details=self.repo.admin_details(primary)
        self.assertEqual(details['revision'],2)
        self.assertEqual(details['delegates'][0]['permissions'],['verify'])
        with self.connect() as c:
            self.assertFalse(c.execute('SELECT verified FROM shadify_artists WHERE id=%s',(self.artist,)).fetchone()[0])
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_app_admin_audit').fetchone()[0],2)

    def test_primary_state_successor_and_audit_immutable_but_security_deactivation_allowed(self):
        for query in ('DELETE FROM shadify_app_admin','UPDATE shadify_app_admin SET primary_user_id=NULL',
                      "UPDATE shadify_app_admin_audit SET action='forged'",'DELETE FROM shadify_app_admin_audit'):
            with self.assertRaises(psycopg.Error):
                with self.connect() as c:c.execute(query)
        with self.connect() as c:c.execute("UPDATE shadify_users SET active=false WHERE auth_account_id='admin'")
        with self.assertRaises(MediaError):self.repo.admin_details(admitted_actor('admin'))
        with self.connect() as c:
            self.assertIsNotNone(c.execute('SELECT primary_user_id FROM shadify_app_admin').fetchone()[0])

    def test_concurrent_primary_transfers_one_success_no_split_primary(self):
        def transfer(target):
            try:
                self.repo.transfer_primary(admitted_actor('admin'),target,expected_primary='admin',delegates='retain')
                return 200
            except MediaError as error:return error.status
        with ThreadPoolExecutor(2) as pool:outcomes=list(pool.map(transfer,('owner-a','owner-b')))
        self.assertEqual(sorted(outcomes),[200,403])
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_app_admin WHERE primary_user_id IS NOT NULL').fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM shadify_app_admin_audit WHERE action='transfer_primary'").fetchone()[0],1)

    def test_revoke_commits_before_waiting_verification_denies_without_write(self):
        primary,delegate=admitted_actor('admin'),admitted_actor('listener')
        self.repo.set_delegate(primary,'listener',['verify'])
        entered=Event()
        def checked(actor):
            result=require_fresh_actor(actor)
            if actor.account_id=='listener':entered.set()
            return result
        with patch('shadify_api.access.require_fresh_actor',side_effect=checked):
            with ThreadPoolExecutor(1) as pool:
                with self.connect() as c:
                    c.execute('SELECT id FROM shadify_artists WHERE id=%s FOR UPDATE',(self.artist,))
                    future=pool.submit(self.repo.page_state,delegate,self.artist,None,admin_only=True,verification=True)
                    self.assertTrue(entered.wait(2))
                    self.repo.set_delegate(primary,'listener',[])
                with self.assertRaises(MediaError) as error:future.result(timeout=5)
                self.assertEqual(error.exception.status,403)
        with self.connect() as c:
            self.assertFalse(c.execute('SELECT verified FROM shadify_artists WHERE id=%s',(self.artist,)).fetchone()[0])

    def test_grant_revocation_serializes_with_authorized_commit(self):
        primary,delegate=admitted_actor('admin'),admitted_actor('listener')
        self.repo.set_delegate(primary,'listener',['verify'])
        begun,finished=Event(),Event()
        def revoke():
            begun.set()
            self.repo.set_delegate(primary,'listener',[])
            finished.set()
        with ThreadPoolExecutor(1) as pool:
            with self.repo.authorized(delegate,self.artist,admin_only=True,operation='verify') as (c,_,_,_):
                c.execute('UPDATE shadify_artists SET verified=true WHERE id=%s',(self.artist,))
                future=pool.submit(revoke)
                self.assertTrue(begun.wait(2))
                self.assertFalse(finished.wait(.15))
            future.result(timeout=5)
        with self.assertRaises(MediaError):self.repo.page_state(delegate,self.artist,None,admin_only=True,verification=False)

    def test_expiry_after_admin_audit_rolls_back_each_authority_mutation(self):
        now=int(time.time());clock=[now]
        primary=admitted_actor('admin',issued=now,expires=now+1)
        original=self.repo._admin_audit
        def audit(*args,**kwargs):
            original(*args,**kwargs)
            clock[0]=primary.expires_at
        for action in (lambda:self.repo.set_delegate(primary,'listener',['verify']),
                       lambda:self.repo.transfer_primary(primary,'owner-a',expected_primary='admin',delegates='revoke'),
                       lambda:self.repo.page_state(primary,self.artist,None,admin_only=True,verification=True)):
            clock[0]=now
            with patch('shadify_api.auth.time.time',side_effect=lambda:clock[0]),patch.object(self.repo,'_admin_audit',side_effect=audit):
                with self.assertRaises(MediaError) as error:action()
                self.assertEqual(error.exception.status,401)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT revision FROM shadify_app_admin').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_app_admin_audit').fetchone()[0],1)
            self.assertFalse(c.execute('SELECT verified FROM shadify_artists WHERE id=%s',(self.artist,)).fetchone()[0])

    def test_permission_matrix_uses_existing_http_operations(self):
        primary=admitted_actor('admin');delegate=admitted_actor('listener')
        track=self.repo.content_write(primary,self.artist,'tracks',{'title':'Draft'})['content']['id']
        moment=self.repo.content_write(primary,self.artist,'moments',{'text':'Draft'})['content']['id']
        with self.connect() as c:user=str(c.execute("SELECT id FROM shadify_users WHERE auth_account_id='owner-a'").fetchone()[0])
        client=TestClient(create_app(identity=lambda:delegate,repository=self.repo,domain=DomainService(self.repo)))
        path='/api/artists/'+str(self.artist)
        admin_path='/api/admin/artists/'+str(self.artist)
        attempts={
          'create_artist':lambda:client.post('/api/admin/artists',json={'name':'New','slug':'new-'+uuid4().hex}),
          'edit_profile':lambda:client.patch(path,json={'bio':'Edited'}),
          'manage_content':lambda:client.patch(path+'/tracks/'+track,json={'title':'Changed'}),
          'publish_content':lambda:client.put(path+'/moments/'+moment+'/publication',json={'state':'published'}),
          'change_owner':lambda:client.post(admin_path+'/owner',json={'account_id':'owner-a'}),
          'suspend':lambda:client.put(admin_path+'/suspension',json={'state':'draft'}),
          'verify':lambda:client.put(admin_path+'/verification',json={'verified':True}),
          'edit_user_profile':lambda:client.patch('/api/admin/users/'+user+'/profile',json={'name':'Target','bio':'Edited'})}
        for granted in sorted(PERMISSIONS):
            self.repo.set_delegate(primary,'listener',[granted])
            with self.subTest(granted=granted):
                for operation,attempt in attempts.items():
                    response=attempt()
                    if operation==granted:
                        self.assertIn(response.status_code,(200,201),(operation,response.text))
                        if operation=='change_owner':self.repo.change_owner(primary,self.artist,action='revoke',expected='owner-a')
                    else:
                        self.assertIn(response.status_code,(403,404),(operation,response.text))
                if granted=='verify':
                    self.assertEqual(client.get(path+'/tracks/'+track).status_code,404)
                    self.assertEqual(client.get('/api/admin/users/'+user+'/profile').status_code,403)
        self.assertEqual(client.put('/api/admin/delegates/owner-b',json={'permissions':['verify']}).status_code,403)
        self.assertEqual(client.put('/api/admin/primary',json={'account_id':'listener','expected_primary_account_id':'admin','delegates':'retain'}).status_code,403)

    def test_primary_http_management_routes_and_explicit_transfer_fields(self):
        actor=['admin']
        client=TestClient(create_app(identity=lambda:admitted_actor(actor[0]),repository=self.repo))
        self.assertEqual(client.get('/api/admin/authority').status_code,200)
        self.assertEqual(client.put('/api/admin/delegates/listener',json={'permissions':['verify']}).status_code,200)
        for body in ({'permissions':['admin']},{'permissions':['verify','verify']},{'permissions':['verify'],'role':'admin'}):
            self.assertEqual(client.put('/api/admin/delegates/listener',json=body).status_code,422)
        self.assertEqual(client.put('/api/admin/primary',json={'account_id':'owner-b','expected_primary_account_id':'admin'}).status_code,422)
        response=client.put('/api/admin/primary',json={'account_id':'owner-b','expected_primary_account_id':'admin','delegates':'retain'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(client.get('/api/admin/authority').status_code,403)
        actor[0]='owner-b'
        self.assertEqual(client.delete('/api/admin/delegates/listener').status_code,200)
        self.assertEqual(len(client.get('/api/admin/authority/audit').json()['events']),4)

    def test_owner_with_narrow_admin_uses_independent_membership_permissions(self):
        self.assign(target='listener')
        primary,delegate=admitted_actor('admin'),admitted_actor('listener')
        self.repo.set_delegate(primary,'listener',['verify'])
        edited=self.repo.edit_page(delegate,self.artist,{'bio':'Own page'})
        self.assertFalse(edited['acting_as_admin'])
        self.assertTrue(edited['capabilities']['manage_content'])
        verified=self.repo.page_state(delegate,self.other,None,admin_only=True,verification=True)
        self.assertTrue(verified['acting_as_admin'])
        self.assertFalse(verified['capabilities']['manage_content'])
        self.repo.set_delegate(primary,'listener',[])
        self.repo.edit_page(delegate,self.artist,{'bio':'Still own page'})
        with self.assertRaises(MediaError):self.repo.edit_page(delegate,self.other,{'bio':'No'})

    def test_suspension_does_not_bypass_separate_publication_permission(self):
        primary,delegate=admitted_actor('admin'),admitted_actor('listener')
        self.repo.set_delegate(primary,'listener',['suspend'])
        self.repo.page_state(delegate,self.artist,'suspended',admin_only=True)
        with self.assertRaises(MediaError) as error:self.repo.page_state(delegate,self.artist,'published',admin_only=True)
        self.assertEqual(error.exception.status,403)
        self.repo.page_state(delegate,self.artist,'draft',admin_only=True)
        self.repo.set_delegate(primary,'listener',['suspend','publish_content'])
        self.repo.page_state(delegate,self.artist,'published',admin_only=True)

    def test_documented_runtime_table_privileges_suffice_and_legacy_grants_unreadable(self):
        from psycopg import sql
        role='synthetic_'+uuid4().hex
        grants={
          'SELECT,INSERT,UPDATE': ['shadify_users','shadify_artists','shadify_media_assets','artist_memberships'],
          'SELECT,INSERT,UPDATE,DELETE': ['shadify_tracks','shadify_releases','shadify_videos','shadify_moments','shadify_events','shadify_app_admin_delegates'],
          'SELECT,INSERT,DELETE': ['shadify_release_tracks'],
          'SELECT,INSERT': ['artist_ownership_audit','shadify_app_admin_audit'],
          'SELECT,UPDATE': ['shadify_app_admin']}
        with self.connect() as c:
            c.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
            c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(self.schema),sql.Identifier(role)))
            for privileges,tables in grants.items():
                c.execute(sql.SQL('GRANT '+privileges+' ON '+','.join(tables)+' TO {}').format(sql.Identifier(role)))
        def connect():
            connection=self.connect()
            connection.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
            return connection
        repo=PostgresMediaRepository(connect)
        primary=admitted_actor('admin')
        self.assertTrue(repo.ready())
        repo.observe_identity(primary)
        repo.set_delegate(primary,'listener',['verify'])
        repo.page_state(admitted_actor('listener'),self.artist,None,admin_only=True,verification=True)
        repo.change_owner(primary,self.artist,action='assign',target='owner-a')
        repo.transfer_primary(primary,'owner-b',expected_primary='admin',delegates='retain')
        self.assertEqual(repo.admin_details(admitted_actor('owner-b'))['primary_account_id'],'owner-b')
        for query in ('SELECT * FROM shadify_admin_grants','DELETE FROM shadify_app_admin_audit','DELETE FROM artist_ownership_audit',
                      'DELETE FROM shadify_app_admin','TRUNCATE shadify_app_admin_delegates'):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with connect() as c:c.execute(query)

    def test_old_central_claim_and_historical_grant_confer_no_power(self):
        from test_integration import identity_request
        from shadify_api.auth import identity_from_admitted_authentication
        claims={'version':1,'domain_app':'shadify','proxy_app':'shadify_api','roles':['admin']}
        actor=identity_from_admitted_authentication(identity_request(changes={'app_authority':claims}))
        self.repo.observe_identity(actor)
        with self.connect() as c:
            c.execute("INSERT INTO shadify_admin_grants(user_id,granted_by) SELECT id,'inert-test-history' FROM shadify_users WHERE auth_account_id='owner'")
        client=TestClient(create_app(identity=lambda:actor,repository=self.repo))
        self.assertEqual(client.post('/api/admin/artists',json={'slug':'denied-claim','name':'Denied'}).status_code,403)
        self.assertEqual(client.put('/api/admin/artists/'+str(self.artist)+'/verification',json={'verified':True}).status_code,403)
        self.assertFalse(client.get('/api/me/artists').json()['acting_as_admin'])


class UnconfiguredAdminTests(ArtistPostgresCase):
    def setUp(self):
        super().setUp()
        # Fresh authority migration, preserving existing identities/artists.
        # Drop only isolated new tables/functions and reapply 006 from source.
        with self.connect() as c:
            c.execute('DROP TABLE shadify_app_admin_audit,shadify_app_admin_delegates,shadify_app_admin')
            c.execute('DROP FUNCTION protect_shadify_primary()')
        self.apply('006_app_admin.sql')

    def test_unconfigured_has_no_primary_first_user_or_legacy_grant_promotion(self):
        with self.connect() as c:
            c.execute("INSERT INTO shadify_admin_grants(user_id,granted_by) SELECT id,'inert-history' FROM shadify_users WHERE auth_account_id='listener'")
        self.repo.observe_identity(admitted_actor('listener'))
        for actor in ('admin','listener'):
            with self.assertRaises(MediaError):self.repo.change_owner(admitted_actor(actor),self.artist,action='assign',target='owner-a')
        with self.assertRaises(MediaError):self.repo.bootstrap_primary(admitted_actor('unknown'),operator_reference='isolated')
        with self.connect() as c:self.assertIsNone(c.execute('SELECT primary_user_id FROM shadify_app_admin').fetchone()[0])
        self.repo.bootstrap_primary(admitted_actor('listener'),operator_reference='explicit-synthetic-operator')
        with self.assertRaises(MediaError):self.repo.bootstrap_primary(admitted_actor('admin'),operator_reference='isolated')
        self.repo.change_owner(admitted_actor('listener'),self.artist,action='assign',target='owner-a')
