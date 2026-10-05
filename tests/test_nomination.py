"""Unwired nomination SQL core; explicitly synthetic trust, no contact/tag issuer."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import UUID, uuid4
from unittest.mock import patch
import psycopg
from authority_fixture import admitted_actor
from shadify_api.domain import DomainService
from shadify_api.main import create_app
from shadify_api.media import MediaError
from shadify_api.nomination import NominationRepository, NominationScope, NominationCommand, VerifiedContacts
from shadify_api.repository import PostgresMediaRepository
from fastapi.testclient import TestClient
from test_artist_postgres import ArtistPostgresCase


class SyntheticTrust:
    approved = True
    evidence = None
    entered = None
    def authorize_change(self, actor, command): return self.approved
    def verified_contacts(self, actor, scope):
        proof = self.evidence or VerifiedContacts(actor.account_id, scope,
                   'synthetic-email-selector', 'synthetic-phone-selector', True, True, actor.expires_at)
        if self.entered: self.entered.set()
        return proof


class NominationTests(ArtistPostgresCase):
    def setUp(self):
        super().setUp()
        self.assign()  # Preserve independent artist ownership before isolated authority reset.
        with self.connect() as c:
            c.execute('DROP TABLE shadify_app_admin_audit,shadify_app_admin_delegates,shadify_app_admin')
            c.execute('DROP FUNCTION protect_shadify_primary()')
        self.apply('006_app_admin.sql','007_admin_nomination.sql')
        self.scope = NominationScope(uuid4(), 'isolated-WSL-fixture')
        self.trust = SyntheticTrust()
        self.nom = NominationRepository(self.connect, self.scope, trust=self.trust, max_ttl_seconds=3600)
        self.operator = admitted_actor('admin')
        self.target = admitted_actor('owner-a')

    def command(self, **changes):
        values = dict(operation_id=uuid4(), nomination_id=uuid4(), scope=self.scope, action='register',
                      expected_nomination_revision=0, expected_admin_revision=0, mode='initialize_primary',
                      email_tag='synthetic-email-selector',phone_tag='synthetic-phone-selector',expires_at=int(time.time())+300)
        values.update(changes)
        return NominationCommand(**values)

    def claim(self, command, actor=None, revision=1, **kwargs):
        return self.nom.claim_nomination(actor or self.target, self.scope, command.nomination_id,
                                        expected_revision=revision, operation_id=kwargs.get('operation_id',uuid4()))

    def primary(self):
        with self.connect() as c:
            return c.execute('SELECT u.auth_account_id FROM shadify_app_admin a LEFT JOIN shadify_users u ON u.id=a.primary_user_id').fetchone()[0]

    def row(self, command):
        from psycopg.rows import dict_row
        with self.connect() as c:
            with c.cursor(row_factory=dict_row) as cursor:
                return cursor.execute('SELECT * FROM shadify_admin_nominations WHERE id=%s',(command.nomination_id,)).fetchone()

    def test_explicit_pending_and_claim_stable_identity_without_first_user_grant(self):
        command=self.command()
        pending=self.nom.change_nomination(self.operator,command)
        self.assertEqual(pending['state'],'pending'); self.assertIsNone(self.primary())
        self.assertNotIn('email_tag',pending); self.assertNotIn('phone_tag',pending)
        PostgresMediaRepository(self.connect).observe_identity(admitted_actor('listener'))
        self.assertIsNone(self.primary())
        result=self.claim(command)
        self.assertEqual(result['state'],'claimed'); self.assertEqual(self.primary(),'owner-a')
        with self.connect() as c:
            stable=c.execute("SELECT id FROM shadify_users WHERE auth_account_id='owner-a'").fetchone()[0]
            self.assertEqual(self.row(command)['claimed_user_id'],stable)
            audits=c.execute('SELECT action,details FROM shadify_app_admin_audit').fetchall()
        self.assertEqual({r[0] for r in audits},{'nomination_register','nomination_claim'})
        self.assertNotIn('synthetic-email-selector',json.dumps(audits))
        self.assertNotIn('synthetic-phone-selector',json.dumps(audits))
        self.trust.evidence=VerifiedContacts('owner-a',self.scope,'changed-email','changed-phone',True,True,self.target.expires_at)
        self.assertEqual(self.primary(),'owner-a')  # primary binds stable account, not future contact text

    def test_both_tags_same_account_verified_and_scope_required(self):
        command=self.command(); self.nom.change_nomination(self.operator,command)
        good=VerifiedContacts('owner-a',self.scope,command.email_tag,command.phone_tag,True,True,self.target.expires_at)
        for changes in ({'email_tag':'wrong'},{'phone_tag':'wrong'},{'email_verified':False},{'phone_verified':False},
                        {'account_id':'owner-b'},{'scope':replace(self.scope,deployment='other')},
                        {'expires_at':int(time.time())},{'expires_at':self.target.expires_at+1}):
            self.trust.evidence=replace(good,**changes)
            with self.assertRaises(MediaError): self.claim(command)
            self.assertIsNone(self.primary()); self.assertEqual(self.row(command)['state'],'pending')
        self.trust.evidence=good; self.assertEqual(self.claim(command)['primary_account_id'],'owner-a')

    def test_no_trust_or_human_approval_cannot_register_or_claim(self):
        command=self.command(); self.trust.approved=False
        with self.assertRaises(MediaError) as error:self.nom.change_nomination(self.operator,command)
        self.assertEqual(error.exception.status,403)
        closed=NominationRepository(self.connect,self.scope,max_ttl_seconds=3600)
        with self.assertRaises(MediaError) as error:closed.change_nomination(self.operator,command)
        self.assertEqual(error.exception.status,503)
        self.trust.approved=True; self.nom.change_nomination(self.operator,command)
        with self.assertRaises(MediaError):closed.claim_nomination(self.target,self.scope,command.nomination_id,expected_revision=1,operation_id=uuid4())
        self.assertIsNone(self.primary())

    def test_replace_and_renew_cas_do_not_grant_and_old_selector_denied(self):
        command=self.command(); self.nom.change_nomination(self.operator,command)
        changed=replace(command,action='replace',operation_id=uuid4(),expected_nomination_revision=1,email_tag='replacement-email')
        self.assertEqual(self.nom.change_nomination(self.operator,changed)['revision'],2)
        with self.assertRaises(MediaError):self.claim(command,revision=1)
        with self.assertRaises(MediaError):self.claim(command,revision=2)
        invalid=replace(changed,action='renew',operation_id=uuid4(),expected_nomination_revision=2,phone_tag='new-selector')
        with self.assertRaises(MediaError):self.nom.change_nomination(self.operator,invalid)
        renewed=replace(changed,action='renew',operation_id=uuid4(),expected_nomination_revision=2,expires_at=command.expires_at+30)
        self.assertEqual(self.nom.change_nomination(self.operator,renewed)['revision'],3)
        self.trust.evidence=VerifiedContacts('owner-a',self.scope,'replacement-email',command.phone_tag,True,True,self.target.expires_at)
        self.assertEqual(self.claim(command,revision=3)['state'],'claimed')

    def test_revoked_and_consumed_intents_never_regrant_or_clear_primary(self):
        command=self.command(); self.nom.change_nomination(self.operator,command)
        revoke=replace(command,action='revoke',operation_id=uuid4(),expected_nomination_revision=1)
        self.assertEqual(self.nom.change_nomination(self.operator,revoke)['state'],'revoked')
        with self.assertRaises(MediaError):self.claim(command,revision=2)
        with self.assertRaises(MediaError):self.nom.change_nomination(self.operator,replace(revoke,action='renew',operation_id=uuid4(),expected_nomination_revision=2))
        other=self.command();self.nom.change_nomination(self.operator,other);self.claim(other)
        self.primary_fixture('owner-b',delegates='revoke')
        with self.assertRaises(MediaError):self.claim(other,revision=2)
        self.assertEqual(self.primary(),'owner-b')
        for sql in ('DELETE FROM shadify_admin_nominations',"UPDATE shadify_admin_nominations SET state='pending',claimed_user_id=NULL,revision=revision+1"):
            with self.assertRaises(psycopg.Error):
                with self.connect() as c:c.execute(sql)

    def test_expired_intent_fails_claim_and_explicit_renew_can_refresh(self):
        command=self.command();self.nom.change_nomination(self.operator,command)
        with self.connect() as c:
            c.execute("UPDATE shadify_admin_nominations SET expires_at=clock_timestamp()-interval '1 second',revision=revision+1 WHERE id=%s",(command.nomination_id,))
        with self.assertRaises(MediaError):self.claim(command,revision=2)
        self.assertEqual(self.nom._dto(self.row(command))['state'],'expired')
        renew=replace(command,action='renew',operation_id=uuid4(),expected_nomination_revision=2)
        self.assertEqual(self.nom.change_nomination(self.operator,renew)['revision'],3)
        self.claim(command,revision=3); self.assertEqual(self.primary(),'owner-a')

    def test_register_expired_history_preserved_and_foreign_scope_not_guessed(self):
        command=self.command();self.nom.change_nomination(self.operator,command)
        with self.connect() as c:
            c.execute("UPDATE shadify_admin_nominations SET expires_at=clock_timestamp()-interval '1 second',revision=revision+1 WHERE id=%s",(command.nomination_id,))
        foreign=NominationRepository(self.connect,replace(self.scope,app_instance_id=uuid4()),trust=self.trust,max_ttl_seconds=3600)
        with self.assertRaises(MediaError):foreign.change_nomination(self.operator,replace(self.command(),scope=foreign.scope))
        new=self.command();self.nom.change_nomination(self.operator,new)
        self.assertEqual(self.row(command)['state'],'expired'); self.assertIsNone(self.primary())

    def test_recover_inactive_primary_explicit_retain_delegates_and_owner_independent(self):
        self.nom.bootstrap_primary(self.operator,operator_reference='synthetic-only')
        self.nom.set_delegate(self.operator,'listener',['verify'], expected_revision=self.admin_revision());self.nom.set_delegate(self.operator,'owner-b',['manage_content'], expected_revision=self.admin_revision())
        command=self.command(mode='recover_primary',expected_primary_account_id='admin',expected_admin_revision=3,delegates='retain')
        self.nom.change_nomination(self.operator,command)
        with self.connect() as c:c.execute("UPDATE shadify_users SET active=false WHERE auth_account_id='admin'")
        self.assertEqual(self.claim(command,admitted_actor('owner-b'))['primary_account_id'],'owner-b')
        details=self.nom.admin_details(admitted_actor('owner-b'))
        self.assertEqual(details['delegates'],[{'auth_account_id':'listener','permissions':['verify'],'active':True}])
        with PostgresMediaRepository(self.connect).authorized(self.target,self.artist,operation='edit_profile') as (_,auth,_,_):
            self.assertTrue(auth.owner);self.assertFalse(auth.admin)
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM artist_ownership_audit').fetchone()[0],1)

    def test_recovery_revoke_and_revision_drift_deny_before_claim(self):
        self.nom.bootstrap_primary(self.operator,operator_reference='synthetic-only')
        self.nom.set_delegate(self.operator,'listener',['verify'], expected_revision=self.admin_revision())
        command=self.command(mode='recover_primary',expected_primary_account_id='admin',expected_admin_revision=2,delegates='revoke')
        self.nom.change_nomination(self.operator,command)
        self.nom.set_delegate(self.operator,'listener',['suspend'], expected_revision=self.admin_revision())
        with self.assertRaises(MediaError):self.claim(command)
        replacement=replace(command,action='replace',operation_id=uuid4(),expected_nomination_revision=1,expected_admin_revision=3)
        self.nom.change_nomination(self.operator,replacement);self.claim(command,revision=2)
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_app_admin_delegates').fetchone()[0],0)

    def test_unknown_unproven_inactive_target_no_identity_creation(self):
        command=self.command();self.nom.change_nomination(self.operator,command)
        for account in ('unknown','unproven','listener'):
            if account=='unproven':
                with self.connect() as c:c.execute("INSERT INTO shadify_users(id,auth_account_id) VALUES (%s,'unproven')",(uuid4(),))
            if account=='listener':
                with self.connect() as c:c.execute("UPDATE shadify_users SET active=false WHERE auth_account_id='listener'")
            with self.assertRaises(MediaError):self.claim(command,admitted_actor(account))
        self.assertIsNone(self.primary())
        with self.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM shadify_users WHERE auth_account_id='unknown'").fetchone()[0],0)

    def test_concurrent_registration_and_claim_single_winner(self):
        def register(command):
            try:self.nom.change_nomination(self.operator,command);return 200,command
            except MediaError as error:return error.status,command
        with ThreadPoolExecutor(2) as pool:outcomes=list(pool.map(register,[self.command(),self.command()]))
        self.assertEqual(sorted(r[0] for r in outcomes),[200,409]);command=next(c for status,c in outcomes if status==200)
        def claim(_):
            try:self.claim(command);return 200
            except MediaError as error:return error.status
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(claim,range(2)))
        self.assertEqual(sorted(results),[200,409]);self.assertEqual(self.primary(),'owner-a')
        with self.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM shadify_app_admin_audit WHERE action='nomination_claim'").fetchone()[0],1)

    def test_audit_failure_rolls_back_register_and_claim(self):
        command=self.command();self.nom.change_nomination(self.operator,command)
        with self.connect() as c:
            c.execute("CREATE FUNCTION reject_nom_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic'; END $$")
            c.execute('CREATE TRIGGER reject_nom_audit BEFORE INSERT ON shadify_app_admin_audit FOR EACH ROW EXECUTE FUNCTION reject_nom_audit()')
        with self.assertRaises(psycopg.Error):self.claim(command)
        self.assertIsNone(self.primary());self.assertEqual(self.row(command)['state'],'pending')
        replacement=replace(command,action='replace',operation_id=uuid4(),expected_nomination_revision=1,email_tag='new-tag')
        with self.assertRaises(psycopg.Error):self.nom.change_nomination(self.operator,replacement)
        self.assertEqual(self.row(command)['email_tag'],command.email_tag)

    def test_identity_expiry_after_audit_rolls_back_register_and_claim(self):
        now=int(time.time());clock=[now]
        actor=admitted_actor('admin',issued=now,expires=now+1)
        original=self.nom._admin_audit
        def audit(*args,**kwargs):original(*args,**kwargs);clock[0]=now+1
        command=self.command()
        with patch('shadify_api.auth.time.time',side_effect=lambda:clock[0]),patch.object(self.nom,'_admin_audit',side_effect=audit):
            with self.assertRaises(MediaError):self.nom.change_nomination(actor,command)
        self.assertIsNone(self.row(command));clock[0]=now
        self.nom.change_nomination(self.operator,command)
        actor=admitted_actor('owner-a',issued=now,expires=now+1)
        with patch('shadify_api.auth.time.time',side_effect=lambda:clock[0]),patch.object(self.nom,'_admin_audit',side_effect=audit):
            with self.assertRaises(MediaError):self.claim(command,actor)
        self.assertIsNone(self.primary());self.assertEqual(self.row(command)['state'],'pending')

    def test_contact_proof_expires_during_real_admin_lock_wait(self):
        command=self.command();self.nom.change_nomination(self.operator,command)
        now=int(time.time());clock=[now];self.trust.entered=Event()
        actor=admitted_actor('owner-a',issued=now,expires=now+1)
        with patch('shadify_api.auth.time.time',side_effect=lambda:clock[0]):
            with ThreadPoolExecutor(1) as pool:
                with self.connect() as c:
                    c.execute('SELECT singleton FROM shadify_app_admin FOR UPDATE')
                    future=pool.submit(self.claim,command,actor)
                    self.assertTrue(self.trust.entered.wait(2));self.assertFalse(future.done());clock[0]=now+1
                with self.assertRaises(MediaError):future.result(timeout=5)
        self.assertIsNone(self.primary());self.assertEqual(self.row(command)['state'],'pending')

    def test_duplicate_operation_id_atomic_and_invalid_policy_scope_rejected(self):
        command=self.command();self.nom.change_nomination(self.operator,command)
        renew=replace(command,action='renew',expected_nomination_revision=1)
        with self.assertRaises(MediaError):self.nom.change_nomination(self.operator,renew)
        self.assertEqual(self.row(command)['revision'],1)
        for changes in ({'scope':replace(self.scope,app='other')},{'email_tag':''},{'phone_tag':''},
                        {'expires_at':int(time.time())+3601},{'expected_admin_revision':True},
                        {'mode':'recover_primary','expected_primary_account_id':'admin','delegates':None}):
            with self.assertRaises(MediaError):self.nom.change_nomination(self.operator,replace(self.command(),**changes))
        with self.assertRaises(MediaError):self.claim(command,operation_id=command.operation_id)
        self.assertIsNone(self.primary());self.assertEqual(self.row(command)['state'],'pending')

    def test_unwired_me_and_catalog_ordinary_flows_unchanged(self):
        from shadify_api.security import receiver_catalog
        repo=PostgresMediaRepository(self.connect)
        client=TestClient(create_app(identity=lambda:self.target,repository=repo,domain=DomainService(repo)))
        before=client.get('/api/me').json()
        command=self.command();self.nom.change_nomination(self.operator,command)
        self.assertEqual(before,client.get('/api/me').json())
        self.assertEqual(len(receiver_catalog()),58)
        self.assertEqual(client.post('/api/admin/nominations',json={}).status_code,422)
        self.assertTrue(repo.ready())  # current006 readiness is intentionally unaffected
