"""Real /me handler + disposable SQL; admission/projection inputs synthetic."""
import json,time
from unittest.mock import patch
import psycopg
from fastapi import Request
from fastapi.testclient import TestClient
from shadify_api.auth import AdmittedIdentity
from shadify_api.domain import DomainService
from shadify_api.main import create_app
from shadify_api.repository import PostgresMediaRepository
from test_nomination_control import NominationControlCase

class OptionalNominationMeTests(NominationControlCase):
    def setUp(self):
        super().setUp()
        self.command_pending=self.command();self.apply_command(self.command_pending)
        self.profile=PostgresMediaRepository(self.connect)
    def client(self,*,projection=True):
        payload={'verified_contacts_v1':self.contacts().model_dump(mode='json')} if projection else {}
        def identity(request:Request):
            request.state.admitted_identity=AdmittedIdentity(self.target,json.dumps(payload).encode())
            return self.target
        return TestClient(create_app(identity=identity,domain=DomainService(self.profile),repository=self.profile,nomination=self.nom))
    def snapshot(self):
        with self.connect() as c:
            return (self.row(self.command_pending.nomination_id),
                    c.execute('SELECT primary_user_id,revision FROM shadify_app_admin').fetchone(),
                    c.execute('SELECT count(*) FROM shadify_app_admin_audit').fetchone()[0],
                    c.execute('SELECT count(*) FROM shadify_nomination_operations').fetchone()[0])
    def ordinary(self,response,before):
        self.assertEqual(response.status_code,200,response.text)
        body=response.json();self.assertEqual(body['account_id'],'owner-a')
        self.assertEqual(body['artist_ids'],[str(self.artist)])
        self.assertEqual(body['app_admin'],{'is_primary':False,'permissions':[],'can_manage_admin':False})
        self.assertNotIn('claim',body);self.assertEqual(self.snapshot(),before)
        self.assertIsNone(self.primary())
    def test_absent_projection_does_not_run_failing_nomination(self):
        before=self.snapshot()
        with patch.object(self.nom,'finalize',side_effect=RuntimeError('nomination unavailable')) as finalize:
            self.ordinary(self.client(projection=False).get('/api/me'),before)
            finalize.assert_not_called()
    def test_nomination_connection_failure_preserves_ordinary_response(self):
        before=self.snapshot()
        with patch.object(self.nom,'connect',side_effect=psycopg.OperationalError('synthetic nomination-only failure')):
            response=self.client().get('/api/me')
        self.ordinary(response,before)
    def test_audit_failure_rolls_back_primary_nomination_and_audit_before_profile(self):
        before=self.snapshot();original=self.nom._admin_audit
        def failed(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('synthetic failure after claim and audit writes')
        with patch.object(self.nom,'_admin_audit',side_effect=failed):response=self.client().get('/api/me')
        self.ordinary(response,before)
    def test_real_nomination_row_lock_timeout_preserves_ordinary_response(self):
        before=self.snapshot();connect=self.connect
        def timed():
            connection=connect();connection.execute("SET LOCAL lock_timeout='50ms'");return connection
        with connect() as blocker:
            blocker.execute('SELECT singleton FROM shadify_nomination_control FOR UPDATE')
            with patch.object(self.nom,'connect',side_effect=timed):response=self.client().get('/api/me')
        self.ordinary(response,before)
    def test_identity_expiring_during_optional_failure_still_denies(self):
        before=self.snapshot();clock=[int(time.time())]
        def failed():
            clock[0]=self.target.expires_at
            raise psycopg.OperationalError('synthetic nomination-only failure')
        with patch('shadify_api.auth.time.time',side_effect=lambda:clock[0]),patch.object(self.nom,'connect',side_effect=failed):
            response=self.client().get('/api/me')
        self.assertEqual(response.status_code,401,response.text)
        self.assertNotIn('app_admin',response.json());self.assertEqual(self.snapshot(),before)
