"""Isolated SQL/HTTP CAS and ordinary-transfer closure; synthetic Auth only."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Barrier, Event
from unittest.mock import patch
from fastapi.testclient import TestClient
from authority_fixture import admitted_actor
from test_artist_postgres import ArtistPostgresCase
from shadify_api.main import create_app
from shadify_api.repository import PostgresMediaRepository
from shadify_api.media import MediaError


class AdminRevisionTests(ArtistPostgresCase):
    def setUp(self):
        super().setUp();self.repo=PostgresMediaRepository(self.connect)
        self.actor=admitted_actor('admin')
        self.client=TestClient(create_app(identity=lambda:self.actor,repository=self.repo))

    def snapshot(self):
        with self.connect() as c:
            return tuple(c.execute(query).fetchall() for query in (
                'SELECT primary_user_id,revision FROM shadify_app_admin',
                'SELECT user_id,permissions FROM shadify_app_admin_delegates ORDER BY user_id',
                'SELECT id,action,details FROM shadify_app_admin_audit ORDER BY id'))

    def grant(self,permissions,revision):
        return self.repo.set_delegate(self.actor,'listener',permissions,expected_revision=revision)

    def test_stale_http_editor_cannot_restore_newly_revoked_permissions(self):
        path='/api/admin/delegates/listener'
        initial=self.client.get('/api/admin/authority').json()['revision']
        granted=self.client.put(path,json={'permissions':['verify'],'expected_revision':initial})
        self.assertEqual(granted.status_code,200,granted.text);old=granted.json()['revision']
        revoked=self.client.request('DELETE',path,json={'expected_revision':old})
        self.assertEqual(revoked.status_code,200,revoked.text)
        self.assertEqual(revoked.json()['revision'],old+1);before=self.snapshot()
        stale=self.client.put(path,json={'permissions':['verify','suspend'],'expected_revision':old})
        self.assertEqual(stale.status_code,409,stale.text);self.assertEqual(self.snapshot(),before)
        with self.assertRaises(MediaError):
            self.repo.page_state(admitted_actor('listener'),self.artist,None,admin_only=True,verification=True)
        reviewed=self.client.put(path,json={'permissions':['verify'],'expected_revision':old+1})
        self.assertEqual(reviewed.status_code,200,reviewed.text)

    def test_waiting_stale_update_loses_to_committing_revocation(self):
        old=self.grant(['verify'],self.admin_revision())['revision']
        written,release,editor_started=Event(),Event(),Event()
        original=self.repo._admin_audit
        def audit(*args,**kwargs):
            original(*args,**kwargs)
            if kwargs.get('details',{}).get('after')==[]:
                written.set()
                if not release.wait(3):raise AssertionError('fixture release missing')
        def edit():
            editor_started.set();return self.grant(['verify','suspend'],old)
        with patch.object(self.repo,'_admin_audit',side_effect=audit),ThreadPoolExecutor(2) as pool:
            revoke=pool.submit(self.grant,[],old)
            try:
                self.assertTrue(written.wait(2));stale=pool.submit(edit)
                self.assertTrue(editor_started.wait(2))
                with self.assertRaises(TimeoutError):stale.result(timeout=.1)
            finally:release.set()
            self.assertEqual(revoke.result(5)['revision'],old+1)
            with self.assertRaises(MediaError) as error:stale.result(5)
            self.assertEqual(error.exception.status,409)
        details=self.repo.admin_details(self.actor)
        self.assertEqual(details['revision'],old+1);self.assertEqual(details['delegates'],[])
        with self.connect() as c:
            events=c.execute("SELECT details FROM shadify_app_admin_audit WHERE action='set_delegate'").fetchall()
        self.assertEqual(len(events),2)
        self.assertIn({'before':['verify'],'after':[],'expected_revision':old,'revision':old+1},[row[0] for row in events])

    def test_two_same_revision_mutations_have_one_committed_audit(self):
        revision=self.grant(['verify'],self.admin_revision())['revision'];barrier=Barrier(2)
        def mutation(permissions):
            barrier.wait()
            try:return (200,self.grant(permissions,revision))
            except MediaError as error:return (error.status,None)
        with ThreadPoolExecutor(2) as pool:
            outcomes=list(pool.map(mutation,([],['suspend'])))
        self.assertEqual(sorted(code for code,_ in outcomes),[200,409])
        self.assertEqual(self.admin_revision(),revision+1)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM shadify_app_admin_audit WHERE action='set_delegate'").fetchone()[0],2)

    def test_stale_revocation_does_not_erase_a_newer_permission_decision(self):
        old=self.grant(['verify'],self.admin_revision())['revision']
        new=self.grant(['suspend'],old)['revision'];before=self.snapshot()
        stale=self.client.request('DELETE','/api/admin/delegates/listener',json={'expected_revision':old})
        self.assertEqual(stale.status_code,409);self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.repo.admin_details(self.actor)['delegates'][0]['permissions'],['suspend'])
        self.assertEqual(new,old+1)

    def test_missing_or_noninteger_revision_cannot_mutate_via_put_or_delete(self):
        path='/api/admin/delegates/listener';before=self.snapshot()
        for value in (None,True,False,'1',1.0,-1,9223372036854775808):
            for method in ('PUT','DELETE'):
                body={} if value is None else {'expected_revision':value}
                if method=='PUT':body['permissions']=['verify']
                self.assertEqual(self.client.request(method,path,json=body).status_code,422)
        self.assertEqual(self.snapshot(),before)

    def test_ordinary_primary_route_and_repository_are_closed_without_effect(self):
        self.grant(['verify'],self.admin_revision());before=self.snapshot()
        with patch.object(self.repo,'connect',side_effect=AssertionError('DB forbidden')):
            for actor in (self.actor,admitted_actor('listener')):
                with self.assertRaises(MediaError) as error:
                    self.repo.transfer_primary(actor,'owner-a',expected_primary='admin',delegates='retain')
                self.assertEqual(error.exception.status,403)
            for policy in ('retain','revoke'):
                response=self.client.put('/api/admin/primary',json={
                    'account_id':'owner-a','expected_primary_account_id':'admin','delegates':policy})
                self.assertEqual(response.status_code,403,response.text)
        self.assertEqual(self.snapshot(),before)
        self.grant([],self.admin_revision())
        self.assertEqual(self.repo.admin_details(self.actor)['delegates'],[])
