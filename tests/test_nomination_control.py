"""Finalized v1 SQL proof, synthetic evidence; never real operator provisioning."""
import hashlib,json,time
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from psycopg.rows import dict_row
from authority_fixture import admitted_actor
from test_artist_postgres import ArtistPostgresCase
from shadify_api.media import MediaError
from shadify_api.nomination_control import NominationControl
from shadify_api.nomination_contract import Scope,Witness,Approval,Contacts,SelectionProjection,ProjectionBase,parse_operation

TAG='v1:'+('a'*64)
class Reader:
    witness=None
    def read(self):return self.witness

class NominationControlCase(ArtistPostgresCase):
    def setUp(self):
        super().setUp();self.assign()
        with self.connect() as c:
            c.execute('DROP TABLE shadify_app_admin_audit,shadify_app_admin_delegates,shadify_app_admin')
            c.execute('DROP FUNCTION protect_shadify_primary()')
        self.apply('006_app_admin.sql','007_admin_nomination.sql','008_nomination_control.sql')
        self.scope=Scope(app_id='shadify_api',app_instance_id=str(uuid4()),domain_app='shadify',deployment_id='synthetic')
        self.reader=Reader();self.nom=NominationControl(self.connect,lambda:self.scope,self.reader)
        now=int(time.time());self.actor=admitted_actor('admin',issued=now,expires=now+60)
        self.target=admitted_actor('owner-a',issued=now,expires=now+60)
    def command(self,**changes):
        body=dict(operation='register',operation_id=str(uuid4()),nomination_id=str(uuid4()),scope=self.scope.model_dump(),
                  expected_nomination_revision=0,intent='initialize',expected_admin_revision=0,expected_primary_account_id=None,
                  delegate_policy=None,expires_at=int(time.time())+300,selector={'email':'synthetic@example.invalid'})
        body.update(changes);return parse_operation(json.dumps(body).encode())
    def apply_command(self,command,fingerprint=None):
        digest=hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        self.reader.witness=Witness(schema_version=1,scope=self.scope,operation=command.operation,operation_id=command.operation_id,
            nomination_id=command.nomination_id,actor_account_id=self.actor.account_id,actor_session_id=self.actor.session_id,
            body_sha256=digest,expires_at=self.actor.expires_at)
        evidence=Approval(actor=self.actor,scope=self.scope,operation_id=command.operation_id,
            approval_fingerprint=fingerprint or hashlib.sha256(command.operation_id.encode()).hexdigest(),
            expires_at=self.actor.expires_at,witness=self.reader.witness)
        values=dict(scope=self.scope.model_dump(),operation=command.operation,operation_id=command.operation_id,
                    nomination_id=command.nomination_id,body_sha256=digest)
        if command.operation in ('register','replace'):
            values.update(selector_tags={'email':[TAG] if command.selector.email else [],'phone':[TAG] if command.selector.phone else []},existing_account_id='owner-a')
            projection=SelectionProjection.model_validate(values)
        else:projection=ProjectionBase.model_validate(values)
        return self.nom.apply(command,projection,evidence,digest)
    def follow(self,first,operation,revision=1,**changes):
        values=dict(operation=operation,operation_id=str(uuid4()),nomination_id=first.nomination_id,scope=self.scope.model_dump(),expected_nomination_revision=revision)
        values.update(changes);return parse_operation(json.dumps(values).encode())
    def contacts(self,actor=None,email=None,phone=None):
        return Contacts.model_validate({'scope':self.scope.model_dump(),'account_id':(actor or self.target).account_id,
                  'tags':{'email':[TAG] if email is None else email,'phone':[] if phone is None else phone}})
    def primary(self):
        with self.connect() as c:return c.execute('SELECT u.auth_account_id FROM shadify_app_admin a LEFT JOIN shadify_users u ON u.id=a.primary_user_id').fetchone()[0]
    def row(self,nomination_id):
        with self.connect() as c:
            with c.cursor(row_factory=dict_row) as cursor:return cursor.execute('SELECT * FROM shadify_admin_nomination_v1 WHERE id=%s',(nomination_id,)).fetchone()

class NominationControlTests(NominationControlCase):
    def test_pending_is_not_grant_and_claim_preserves_artist_owner(self):
        cmd=self.command();self.assertEqual(self.apply_command(cmd)['state'],'pending');self.assertIsNone(self.primary())
        self.assertTrue(self.nom.finalize(self.target,self.contacts()));self.assertEqual(self.primary(),'owner-a')
        with self.repo.authorized(self.target,self.artist):pass
        self.assertEqual(self.nom.operation_result(cmd.operation_id)['state'],'claimed')
        with self.connect() as c:
            text=json.dumps(c.execute('SELECT details FROM shadify_app_admin_audit').fetchall(),default=str)
        self.assertNotIn(TAG,text);self.assertNotIn('synthetic@example',text)
    def test_dual_kind_and_pin_cannot_reassign_identity(self):
        cmd=self.command(selector={'email':'e','phone':'p'});self.apply_command(cmd)
        self.assertFalse(self.nom.finalize(self.target,self.contacts()))
        other=admitted_actor('owner-b');self.assertFalse(self.nom.finalize(other,self.contacts(other,phone=[TAG])))
        self.assertTrue(self.nom.finalize(self.target,self.contacts(phone=[TAG])))
    def test_version_overlap_required_for_each_selected_kind(self):
        cmd=self.command();self.apply_command(cmd)
        self.assertFalse(self.nom.finalize(self.target,self.contacts(email=['v2:'+('a'*64)])))
        self.assertTrue(self.nom.finalize(self.target,self.contacts(email=[TAG,'v2:'+('b'*64)])))
    def test_renew_expiry_only_and_expired_never_revives(self):
        cmd=self.command();self.apply_command(cmd);before=self.row(cmd.nomination_id)
        renewed=self.follow(cmd,'renew',expires_at=cmd.expires_at+60);self.apply_command(renewed)
        after=self.row(cmd.nomination_id)
        for key in ('email_tags','phone_tags','existing_account_id','intent','expected_admin_revision'):self.assertEqual(before[key],after[key])
        with patch('shadify_api.nomination_control.datetime') as clock:
            from datetime import datetime,timezone
            clock.now.return_value=datetime.fromtimestamp(cmd.expires_at+61,timezone.utc)
            with self.assertRaises(MediaError):self.apply_command(self.follow(cmd,'renew',revision=2,expires_at=cmd.expires_at+120))
    def test_revoke_ignores_primary_drift_and_replay_is_atomic(self):
        cmd=self.command();self.apply_command(cmd)
        self.repo.bootstrap_primary(self.actor,operator_reference='synthetic-test')
        revoke=self.follow(cmd,'revoke');self.assertEqual(self.apply_command(revoke)['state'],'revoked')
        self.assertFalse(self.nom.finalize(self.target,self.contacts()));self.assertEqual(self.primary(),'admin')
        with self.assertRaises(MediaError):self.apply_command(revoke)
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_nomination_operations').fetchone()[0],2)
    def test_fresh_recovery_after_claim_requires_explicit_policy(self):
        cmd=self.command();self.apply_command(cmd);self.nom.finalize(self.target,self.contacts())
        recovery=self.command(intent='recover',expected_admin_revision=1,expected_primary_account_id='owner-a',delegate_policy='revoke')
        self.assertEqual(self.apply_command(recovery)['state'],'pending');self.assertEqual(self.primary(),'owner-a')
    def test_replace_fresh_id_and_claimed_history_not_replaceable(self):
        cmd=self.command();self.apply_command(cmd)
        replacement=self.command(operation='replace',nomination_id=cmd.nomination_id,expected_nomination_revision=1,replacement_nomination_id=str(uuid4()))
        result=self.apply_command(replacement);self.assertEqual(self.row(cmd.nomination_id)['state'],'replaced')
        self.assertEqual(result['nomination_id'],replacement.replacement_nomination_id)
        self.nom.finalize(self.target,self.contacts())
        with self.assertRaises(MediaError):self.apply_command(self.command(operation='replace',nomination_id=replacement.replacement_nomination_id,expected_nomination_revision=2,replacement_nomination_id=str(uuid4())))
    def test_approval_consumption_and_audit_failure_roll_back_together(self):
        cmd=self.command()
        with patch.object(self.nom,'_admin_audit',side_effect=RuntimeError('synthetic audit failure')):
            with self.assertRaises(RuntimeError):self.apply_command(cmd)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_nomination_operations').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_admin_nomination_v1').fetchone()[0],0)
        self.apply_command(cmd)
    def test_identity_expiry_before_commit_rolls_back(self):
        cmd=self.command();original=self.nom._admin_audit;clock=[int(time.time())]
        def expired(*args,**kwargs):
            original(*args,**kwargs);clock[0]=self.actor.expires_at
        with patch('shadify_api.auth.time.time',side_effect=lambda:clock[0]),patch.object(self.nom,'_admin_audit',side_effect=expired):
            with self.assertRaises(MediaError):self.apply_command(cmd)
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_nomination_operations').fetchone()[0],0)
    def test_claim_revoke_race_has_one_winner(self):
        cmd=self.command();self.apply_command(cmd);barrier=Barrier(2)
        def claim():barrier.wait();return self.nom.finalize(self.target,self.contacts())
        def revoke():
            barrier.wait()
            try:return self.apply_command(self.follow(cmd,'revoke'))['state']
            except MediaError:return 'conflict'
        with ThreadPoolExecutor(2) as pool:
            a=pool.submit(claim);b=pool.submit(revoke);claimed=a.result(5);revoked=b.result(5)
        self.assertIn((claimed,revoked),((True,'conflict'),(False,'revoked')))
        self.assertEqual(self.primary(),'owner-a' if claimed else None)
    def test_strict_shapes_and_revisions(self):
        for changes in ({'operation_id':str(uuid4()).upper()},{'expected_nomination_revision':False},{'action':'register'}, {'scope':{**self.scope.model_dump(),'app_id':'mizfood_api'}}):
            with self.assertRaises(MediaError):self.command(**changes)
        cmd=self.command()
        for field in ('selector','intent','expected_admin_revision'):
            with self.assertRaises(MediaError):self.follow(cmd,'renew',expires_at=cmd.expires_at+1,**{field:'unexpected'})

    def test_reused_approval_rolls_back_fresh_operation(self):
        cmd=self.command();fingerprint='b'*64;self.apply_command(cmd,fingerprint)
        self.apply_command(self.follow(cmd,'revoke'))
        fresh=self.command()
        with self.assertRaises(MediaError):self.apply_command(fresh,fingerprint)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_nomination_operations').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_admin_nomination_v1').fetchone()[0],1)
    def test_expired_replacement_preserves_predecessor_bytes(self):
        cmd=self.command();self.apply_command(cmd);before=self.row(cmd.nomination_id)
        from datetime import datetime,timezone
        real=datetime
        class Clock:
            now=staticmethod(lambda tz:real.fromtimestamp(cmd.expires_at+1,timezone.utc))
            fromtimestamp=staticmethod(real.fromtimestamp)
        with patch('shadify_api.nomination_control.datetime',Clock):
            replacement=self.command(operation='replace',nomination_id=cmd.nomination_id,expected_nomination_revision=1,replacement_nomination_id=str(uuid4()),expires_at=cmd.expires_at+120)
            self.apply_command(replacement)
        self.assertEqual(self.row(cmd.nomination_id),before)
        self.assertEqual(self.row(replacement.replacement_nomination_id)['predecessor_id'],before['id'])
    def test_blocked_replacement_requires_new_reviewed_current_prestate(self):
        cmd=self.command();self.apply_command(cmd)
        self.repo.bootstrap_primary(self.actor,operator_reference='synthetic-test')
        self.assertFalse(self.nom.finalize(self.target,self.contacts()));self.assertEqual(self.row(cmd.nomination_id)['state'],'blocked')
        with self.assertRaises(MediaError):self.apply_command(self.command(operation='replace',nomination_id=cmd.nomination_id,expected_nomination_revision=2,replacement_nomination_id=str(uuid4())))
        replacement=self.command(operation='replace',nomination_id=cmd.nomination_id,expected_nomination_revision=2,replacement_nomination_id=str(uuid4()),intent='recover',expected_admin_revision=1,expected_primary_account_id='admin',delegate_policy='retain')
        self.apply_command(replacement);self.assertTrue(self.nom.finalize(self.target,self.contacts()));self.assertEqual(self.primary(),'owner-a')
    def test_recovery_delegate_policy_is_explicit_and_atomic(self):
        self.repo.bootstrap_primary(self.actor,operator_reference='synthetic-test')
        self.repo.set_delegate(self.actor,'listener',permissions=['verify'])
        cmd=self.command(intent='recover',expected_admin_revision=2,expected_primary_account_id='admin',delegate_policy='revoke')
        self.apply_command(cmd);self.assertEqual(self.primary(),'admin')
        self.assertTrue(self.nom.finalize(self.target,self.contacts()))
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_app_admin_delegates').fetchone()[0],0)

    def test_expiry_after_waiting_for_real_primary_lock(self):
        from threading import Event
        from shadify_api.auth import require_fresh_actor
        cmd=self.command();entered=Event();clock=[int(time.time())]
        original=self.nom._fresh
        def checked(evidence):
            result=original(evidence);entered.set();return result
        with patch('shadify_api.auth.time.time',side_effect=lambda:clock[0]),patch.object(self.nom,'_fresh',side_effect=checked):
            with ThreadPoolExecutor(1) as pool:
                with self.connect() as c:
                    c.execute('SELECT singleton FROM shadify_app_admin FOR UPDATE')
                    future=pool.submit(self.apply_command,cmd)
                    self.assertTrue(entered.wait(2));self.assertFalse(future.done());clock[0]=self.actor.expires_at
                with self.assertRaises(MediaError):future.result(5)
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_nomination_operations').fetchone()[0],0)
