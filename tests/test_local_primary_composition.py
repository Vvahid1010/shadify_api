"""Exact frozen common source + isolated SQL, synthetic policy only."""
from dataclasses import replace
import importlib.util
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
from uuid import uuid4
from psycopg.rows import dict_row
from test_artist_postgres import ArtistPostgresCase
import test_local_primary as primary_fixture
from shadify_api.local_primary import DATABASE_FACTS,WSLBinding
from shadify_api.local_primary_composition import LocalPrimaryComposition,ProtectedCompositionBinding
from shadify_api.media import MediaError

spec=importlib.util.spec_from_file_location('shadify_frozen_common_7701286','/tmp/shadify-common-7701286.py')
common=importlib.util.module_from_spec(spec);sys.modules[spec.name]=common;spec.loader.exec_module(common)


class SyntheticSelectionPolicy:
    def __init__(self,target):
        self.target=target;self.expiry=time.time()+60;self.calls=[];self.change=None;self.originals=[]
    def verify_selection(self,request):
        self.calls.append(request)
        result=common.VerifiedSelectionV1(request,self.target,request.request.binding_ref,
            request.domain_app,request.target_mapping,self.expiry,'synthetic-operator',
            'a'*64,True)
        return self.change(result) if self.change else result


class CompositionTests(ArtistPostgresCase):
    command=primary_fixture.LocalPrimaryTests.command
    snapshot=primary_fixture.LocalPrimaryTests.snapshot
    unconfigured=primary_fixture.LocalPrimaryTests.unconfigured
    def setUp(self):
        super().setUp();self.native=(str(uuid4()),'shadify_api',str(uuid4()))
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as c:facts=c.execute(DATABASE_FACTS).fetchone()
        self.binding=WSLBinding(**facts,environment='wsl',app_id='shadify_api',domain_app='shadify',
            node_id=self.native[0],app_instance_id=self.native[2],reference='synthetic-wsl-binding')
        self.target=common.BoundTargetV1('shadify_api',self.native[2],'isolated-database','shadify.example.invalid')
        self.policy=SyntheticSelectionPolicy(self.target)
        self.protected=ProtectedCompositionBinding(self.binding,self.target,self.connect)
        self.composition=LocalPrimaryComposition(common=common,policy=self.policy,binding=self.protected,
            native_identity_provider=lambda:self.native)
        self.wsl=patch.object(common,'_running_on_wsl',return_value=True);self.wsl.start();self.addCleanup(self.wsl.stop)
        self.installed=patch.object(common,'_installed',return_value=True);self.installed.start();self.addCleanup(self.installed.stop)

    def test_missing_policy_or_binding_denies_before_factory(self):
        for composition in (LocalPrimaryComposition(),LocalPrimaryComposition(common=common,binding=self.protected),
                            LocalPrimaryComposition(common=common,policy=self.policy)):
            with self.assertRaises(MediaError) as error:composition.execute(self.command())
            self.assertEqual(error.exception.status,503)
        self.assertEqual(self.policy.calls,[])

    def test_exact_mapping_domain_operator_and_digest_are_preserved_in_real_audit(self):
        originals=[];verify=common.verify_selection_v1
        def observed(*args,**kwargs):
            result=verify(*args,**kwargs);originals.append((kwargs.get('original'),result));return result
        with patch.object(common,'verify_selection_v1',side_effect=observed):
            result=self.composition.execute(self.command(mode='apply'))
        initial=originals[0][1]
        self.assertTrue(all(original is initial for original,_ in originals[1:]))
        self.assertGreater(len(originals),3)
        self.assertEqual(initial.target_mapping.auth_account_ref,'owner-a')
        self.assertEqual(initial.domain_app,'shadify')
        with self.connect() as c:
            actor,target,details=c.execute('SELECT actor_account_id,target_account_id,details FROM shadify_app_admin_audit WHERE id=%s',
                (result['audit_id'],)).fetchone()
        self.assertIsNone(actor);self.assertEqual(target,'owner-a')
        self.assertEqual(details['authority_reference'],'synthetic-operator')
        self.assertEqual(details['audit_authority_sha256'],'a'*64)
        self.assertNotEqual(details['authority_reference'],target)

    def test_dry_run_read_only_and_mode_replay_denies_without_effects(self):
        self.unconfigured();before=self.snapshot();command=self.command(operation='initialize-primary')
        readonly=[];factory=self.connect
        def connect():
            c=factory();readonly.append(c);return c
        self.composition.binding=replace(self.protected,connection_factory=connect)
        original=self.policy.verify_selection
        def verify(request):
            self.assertEqual(readonly[-1].execute('SHOW transaction_read_only').fetchone()[0],'on')
            return original(request)
        with patch.object(self.policy,'verify_selection',side_effect=verify):
            self.assertEqual(self.composition.execute(command)['outcome'],'eligible')
        self.assertEqual(self.snapshot(),before)
        dry=original(common.SelectionRequestV1(common.RequestV1('shadify_api','initialize-primary',
            command.authority_reference,command.binding_reference,command.mapping.local_user_id),
            'shadify',common.AuthLocalMappingV1('owner-a',command.mapping.local_user_id)))
        with patch.object(self.policy,'verify_selection',return_value=dry):
            with self.assertRaises(MediaError):self.composition.execute(command.model_copy(update={'mode':'apply'}))
        self.assertEqual(self.snapshot(),before)

    def test_wrong_actual_transaction_database_denies(self):
        from psycopg import sql
        other='wrong_'+uuid4().hex
        with self.connect() as c:c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(other)))
        factory=self.connect
        def connect():
            c=factory();c.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(other)));c.commit();return c
        self.composition.binding=replace(self.protected,connection_factory=connect)
        before=self.snapshot()
        with self.assertRaises(MediaError) as error:self.composition.execute(self.command(mode='apply'))
        self.assertEqual(error.exception.status,403);self.assertEqual(self.policy.calls,[])
        self.assertEqual(self.snapshot(),before)
        with self.connect() as c:c.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(other)))

    def test_wrong_actual_postgresql_database_denies_before_mapping_or_verifier(self):
        import psycopg
        from psycopg import sql
        name='wrong_'+uuid4().hex
        with psycopg.connect(**self.server.connection,autocommit=True) as c:
            c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            factory=lambda:psycopg.connect(**{**self.server.connection,'dbname':name})
            self.composition.binding=replace(self.protected,connection_factory=factory)
            before=self.snapshot()
            with self.assertRaises(MediaError) as error:self.composition.execute(self.command(mode='apply'))
            self.assertEqual(error.exception.status,403);self.assertEqual(self.policy.calls,[])
            self.assertEqual(self.snapshot(),before)
        finally:
            with psycopg.connect(**self.server.connection,autocommit=True) as c:
                c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))

    def test_revocation_after_audit_rolls_back_all_transaction_effects(self):
        from shadify_api.repository import PostgresMediaRepository
        command=self.command(mode='apply',delegates='revoke');before=self.snapshot()
        audit=PostgresMediaRepository._admin_audit
        def revoke(repository,*args,**kwargs):
            result=audit(repository,*args,**kwargs)
            self.policy.change=lambda r:replace(r,authorized=False)
            return result
        with patch.object(PostgresMediaRepository,'_admin_audit',revoke):
            with self.assertRaises(MediaError) as error:self.composition.execute(command)
        self.assertEqual(error.exception.status,403);self.assertEqual(self.snapshot(),before)

    def test_wrong_mapping_domain_binding_and_eligible_decision_are_not_authority(self):
        before=self.snapshot();command=self.command(mode='apply')
        changes=(lambda r:replace(r,domain_app='chapary'),
            lambda r:replace(r,target_mapping=common.AuthLocalMappingV1('owner-b',str(uuid4()))),
            lambda r:replace(r,binding_ref='other'),lambda r:common.DecisionV1('eligible','common_controls_only'))
        for change in changes:
            self.policy.change=change
            with self.assertRaises(MediaError):self.composition.execute(command)
            self.assertEqual(self.snapshot(),before)
        self.policy.change=None
        wrong=command.model_copy(update={'mapping':command.mapping.model_copy(update={'account_id':'owner-b'})})
        calls=len(self.policy.calls)
        with self.assertRaises(MediaError) as error:self.composition.execute(wrong)
        self.assertEqual(error.exception.status,422);self.assertEqual(len(self.policy.calls),calls)

    def test_binding_operator_digest_or_expiry_change_rolls_back(self):
        command=self.command(mode='apply');before=self.snapshot()
        changes=(lambda r:replace(r,target=replace(r.target,domain='other.example.invalid')),
            lambda r:replace(r,operator_audit_ref='other-operator'),
            lambda r:replace(r,audit_authority_sha256='b'*64),
            lambda r:replace(r,authority_expires_at=r.authority_expires_at+1))
        for change in changes:
            self.policy.calls=[]
            self.policy.change=lambda r:change(r) if len(self.policy.calls)>1 else r
            with self.assertRaises(MediaError):self.composition.execute(command)
            self.assertEqual(self.snapshot(),before)

    def test_expiry_during_real_admin_lock_wait_rolls_back(self):
        command=self.command(mode='apply');before=self.snapshot();connections=[];factory=self.connect
        def connect():
            c=factory();connections.append(c);return c
        self.composition.binding=replace(self.protected,connection_factory=connect)
        clock=[time.time()]
        with patch('shadify_api.local_primary.time.time',side_effect=lambda:clock[0]),self.connect() as lock,ThreadPoolExecutor(1) as pool:
            lock.execute('SELECT * FROM shadify_app_admin FOR UPDATE')
            future=pool.submit(self.composition.execute,command)
            deadline=time.monotonic()+3;waiting=False
            while time.monotonic()<deadline:
                if connections:
                    with self.connect() as c:row=c.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',
                        (connections[0].info.backend_pid,)).fetchone()
                    if row and row[0]=='Lock':waiting=True;break
                time.sleep(.005)
            self.assertTrue(waiting);self.assertGreater(len(self.policy.calls),0)
            clock[0]=self.policy.expiry;lock.commit()
            with self.assertRaises(MediaError) as error:future.result(5)
            self.assertEqual(error.exception.status,403)
        self.assertEqual(self.snapshot(),before)

    def test_unsupported_refs_and_nonzero_initialize_revision_are_not_normalized(self):
        command=self.command()
        for ref in ('bad/path','a'*129):
            with self.assertRaises(MediaError) as error:self.composition.execute(command.model_copy(update={'authority_reference':ref}))
            self.assertEqual(error.exception.status,422)
        self.unconfigured()
        with self.assertRaises(MediaError) as error:self.composition.execute(self.command(operation='initialize-primary',expected_admin_revision=1))
        self.assertEqual(error.exception.status,422);self.assertEqual(self.policy.calls,[])
