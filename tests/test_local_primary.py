"""Disposable SQL proof, synthetic protected local-control context only."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4
import psycopg
from psycopg.rows import dict_row
from authority_fixture import admitted_actor
from test_artist_postgres import ArtistPostgresCase
from shadify_api.repository import PostgresMediaRepository
from shadify_api.media import MediaError
from shadify_api.local_primary import (LocalPrimaryAdapter,LocalPrimaryCommand,
    VerifiedLocalContext,WSLBinding,command_digest,DATABASE_FACTS)


class SyntheticLocalControl:
    """Explicit fixture, not an operational authority/Topology implementation."""
    def __init__(self,binding):
        self.binding=binding;self.admissions=0;self.allowed=True;self.cached=None
    def admit(self,command):
        self.admissions+=1
        if not self.allowed:raise MediaError(403,'Synthetic authority denied')
        if command.authority_reference!='synthetic-authority' or command.binding_reference!=self.binding.reference:
            raise MediaError(403,'Synthetic protected reference unavailable')
        if self.cached is not None:return self.cached
        return VerifiedLocalContext(binding=self.binding,request_sha256=command_digest(command),
            authority_audit_reference='synthetic-reviewed-authority',expires_at=int(time.time())+60,
            immutable_executable=True)
    def require_current(self,command,context):
        if not self.allowed or context.binding!=self.binding:
            raise MediaError(403,'Synthetic protected context changed')


class LocalPrimaryTests(ArtistPostgresCase):
    def setUp(self):
        super().setUp();self.repo=PostgresMediaRepository(self.connect)
        self.native=(str(uuid4()),'shadify_api',str(uuid4()))
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:facts=c.execute(DATABASE_FACTS).fetchone()
        self.binding=WSLBinding(**facts,environment='wsl',app_id='shadify_api',domain_app='shadify',
            node_id=self.native[0],app_instance_id=self.native[2],reference='synthetic-wsl-binding')
        self.control=SyntheticLocalControl(self.binding)
        self.adapter=LocalPrimaryAdapter(self.repo,context_provider=self.control,
            native_identity_provider=lambda:self.native)

    def command(self,target='owner-a',operation='recover-primary',**changes):
        with self.connect() as c:local=str(c.execute('SELECT id FROM shadify_users WHERE auth_account_id=%s',(target,)).fetchone()[0])
        values=dict(operation=operation,operation_id=str(uuid4()),authority_reference='synthetic-authority',
            binding_reference=self.binding.reference,mapping=dict(app_id='shadify_api',domain_app='shadify',
                account_id=target,local_user_id=local),expected_admin_revision=self.admin_revision())
        if operation=='recover-primary':values.update(expected_primary_account_id='admin',delegates='retain')
        values.update(changes);return LocalPrimaryCommand.model_validate(values)

    def unconfigured(self):
        with self.connect() as c:
            c.execute('DROP TABLE shadify_app_admin_audit,shadify_app_admin_delegates,shadify_app_admin')
            c.execute('DROP FUNCTION protect_shadify_primary()')
        self.apply('006_app_admin.sql')  # Isolated fixture only, never adapter DDL.

    def snapshot(self):
        with self.connect() as c:
            return tuple(c.execute(query).fetchall() for query in (
                'SELECT primary_user_id,revision FROM shadify_app_admin',
                'SELECT user_id,permissions FROM shadify_app_admin_delegates ORDER BY user_id',
                'SELECT id,action,details FROM shadify_app_admin_audit ORDER BY id',
                'SELECT id,auth_account_id,active,first_admitted_at,last_admitted_at,admission_source FROM shadify_users ORDER BY id',
                'SELECT * FROM shadify_artists ORDER BY id','SELECT * FROM artist_memberships ORDER BY id'))

    def test_missing_authority_native_or_root_only_denies_both_modes_before_db(self):
        for mode in ('dry-run','apply'):
            command=self.command(mode=mode)
            for adapter in (LocalPrimaryAdapter(self.repo),LocalPrimaryAdapter(self.repo,context_provider=self.control)):
                with patch('os.geteuid',return_value=0),patch.object(self.repo,'connect',side_effect=AssertionError('DB forbidden')):
                    with self.assertRaises(MediaError) as error:adapter.execute(command)
                    self.assertEqual(error.exception.status,503)
            self.control.allowed=False
            with self.assertRaises(MediaError):self.adapter.execute(command)
            self.control.allowed=True

    def test_default_dry_run_readonly_has_zero_account_audit_or_state_writes(self):
        self.unconfigured();command=self.command(operation='initialize-primary');before=self.snapshot()
        self.assertEqual(command.mode,'dry-run')
        original=self.adapter._database;observed=[]
        def database(c,binding):
            observed.append(c.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'])
            return original(c,binding)
        with patch.object(self.repo,'_admin_audit',side_effect=AssertionError('audit forbidden')),             patch.object(self.repo,'_admin_state',side_effect=AssertionError('write lock forbidden')),             patch.object(self.adapter,'_database',side_effect=database):
            result=self.adapter.execute(command)
        self.assertEqual(result['outcome'],'eligible');self.assertIsNone(result['primary_account_id'])
        self.assertIsNone(result['audit_id']);self.assertEqual(result['revision'],0)
        self.assertEqual(observed,['on','on']);self.assertEqual(self.snapshot(),before)

    def test_apply_initializes_once_and_returns_committed_audit_identity(self):
        self.unconfigured();before=self.snapshot();command=self.command(operation='initialize-primary',mode='apply')
        result=self.adapter.execute(command)
        self.assertEqual(result['outcome'],'committed');self.assertEqual(result['revision'],1)
        self.assertEqual(self.repo.admin_details(admitted_actor('owner-a'))['primary_account_id'],'owner-a')
        with self.connect() as c:
            audit=c.execute('SELECT action,target_account_id FROM shadify_app_admin_audit WHERE id=%s',(result['audit_id'],)).fetchone()
            self.assertEqual(audit,('local_initialize_primary','owner-a'))
        self.assertEqual(self.snapshot()[3:],before[3:])
        after=self.snapshot()
        with self.assertRaises(MediaError) as error:self.adapter.execute(command)
        self.assertEqual(error.exception.status,409);self.assertEqual(self.snapshot(),after)

    def test_apply_does_not_reuse_dry_run_authority_or_prestate(self):
        command=self.command();self.control.cached=self.control.admit(command)
        self.assertEqual(self.adapter.execute(command)['outcome'],'eligible')
        apply=command.model_copy(update={'mode':'apply'})
        with self.assertRaises(MediaError) as error:self.adapter.execute(apply)
        self.assertEqual(error.exception.status,403)
        self.control.cached=None
        self.repo.set_delegate(admitted_actor('admin'),'listener',['verify'],expected_revision=self.admin_revision())
        before=self.snapshot()
        with self.assertRaises(MediaError) as error:self.adapter.execute(apply)
        self.assertEqual(error.exception.status,409);self.assertEqual(self.snapshot(),before)

    def test_explicit_retain_revoke_removes_successor_delegate_and_never_grants_old_primary(self):
        for policy in ('retain','revoke'):
            with self.subTest(policy=policy):
                self.primary_fixture('admin',delegates='revoke')
                for target in ('owner-a','listener'):
                    self.repo.set_delegate(admitted_actor('admin'),target,['verify'],expected_revision=self.admin_revision())
                before=self.snapshot()[3:]
                command=self.command(mode='apply',delegates=policy)
                self.adapter.execute(command)
                roster=self.repo.admin_details(admitted_actor('owner-a'))['delegates']
                self.assertEqual([r['auth_account_id'] for r in roster],['listener'] if policy=='retain' else [])
                self.assertEqual(self.snapshot()[3:],before)
                self.assertNotIn('admin',[r['auth_account_id'] for r in roster])

    def test_missing_wrong_app_domain_or_unestablished_mapping_denies(self):
        original=self.command(mode='apply');before=self.snapshot()
        for changes in ({'app_id':'mizfood_api'},{'domain_app':'chapary'}):
            data=original.model_dump();data['mapping'].update(changes)
            with self.assertRaises(ValueError):LocalPrimaryCommand.model_validate(data)
        for changes in ({'local_user_id':str(uuid4())},{'account_id':'unknown'},
                        {'account_id':'owner-b'}):
            mapping=original.mapping.model_copy(update=changes)
            command=original.model_copy(update={'mapping':mapping})
            with self.assertRaises(MediaError) as error:self.adapter.execute(command)
            self.assertEqual(error.exception.status,422)
        self.assertEqual(self.snapshot(),before)
        for sql in ("UPDATE shadify_users SET active=false WHERE auth_account_id='owner-a'",
                    "UPDATE shadify_users SET active=true,first_admitted_at=NULL,last_admitted_at=NULL,admission_source=NULL WHERE auth_account_id='owner-a'"):
            with self.connect() as c:c.execute(sql)
            with self.assertRaises(MediaError):self.adapter.execute(original)

    def test_wrong_database_node_vm_or_mutable_executable_denies(self):
        command=self.command(mode='apply');before=self.snapshot()
        variants=({'database_name':'other'},{'database_oid':self.binding.database_oid+1},
            {'schema_name':'other'},{'postmaster_started_at':self.binding.postmaster_started_at+timedelta(seconds=1)},
            {'node_id':str(uuid4())},{'environment':'vm'},{'app_id':'mizfood_api'})
        for changes in variants:
            self.control.binding=self.binding.model_copy(update=changes)
            with self.assertRaises((MediaError,ValueError)):self.adapter.execute(command)
            self.assertEqual(self.snapshot(),before)
        self.control.binding=self.binding
        proof=self.control.admit(command)
        self.control.cached=proof.model_copy(update={'immutable_executable':False})
        with self.assertRaises(MediaError):self.adapter.execute(command)
        self.assertEqual(self.snapshot(),before)

    def test_caller_dsn_env_dev_node_witness_and_unknown_fields_are_not_inputs(self):
        values=self.command().model_dump()
        for key in ('dsn','database','environment','dev','uid','username','node_witness','instance_id'):
            with self.assertRaises(ValueError):LocalPrimaryCommand.model_validate({**values,key:'untrusted'})
        for changes in ({'expected_primary_account_id':None},{'delegates':None},
                        {'expected_admin_revision':True},{'mode':'execute'}):
            with self.assertRaises(ValueError):LocalPrimaryCommand.model_validate({**values,**changes})

    def test_initialize_and_recovery_races_have_one_winner(self):
        for operation in ('initialize-primary','recover-primary'):
            with self.subTest(operation=operation):
                if operation=='initialize-primary':self.unconfigured()
                else:self.primary_fixture('admin',delegates='revoke')
                commands=[self.command(target=target,operation=operation,mode='apply') for target in ('owner-a','owner-b')]
                revision=self.admin_revision();barrier=Barrier(2)
                def execute(command):
                    barrier.wait()
                    try:return (200,self.adapter.execute(command))
                    except MediaError as error:return (error.status,None)
                with ThreadPoolExecutor(2) as pool:outcomes=list(pool.map(execute,commands))
                self.assertEqual(sorted(code for code,_ in outcomes),[200,409])
                self.assertEqual(self.admin_revision(),revision+1)
                with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_app_admin WHERE primary_user_id IS NOT NULL').fetchone()[0],1)

    def test_audit_failure_rolls_back_primary_policy_and_revision(self):
        self.repo.set_delegate(admitted_actor('admin'),'listener',['verify'],expected_revision=self.admin_revision())
        command=self.command(mode='apply',delegates='revoke');before=self.snapshot()
        with self.connect() as c:
            c.execute("CREATE FUNCTION reject_local_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic audit failure'; END $$")
            c.execute('CREATE TRIGGER reject_local_audit BEFORE INSERT ON shadify_app_admin_audit FOR EACH ROW EXECUTE FUNCTION reject_local_audit()')
        with self.assertRaises(psycopg.Error):self.adapter.execute(command)
        self.assertEqual(self.snapshot(),before)

    def test_ending_authority_or_identity_change_rolls_back_every_effect(self):
        self.repo.set_delegate(admitted_actor('admin'),'listener',['verify'],expected_revision=self.admin_revision())
        command=self.command(mode='apply',delegates='revoke');before=self.snapshot();original=self.repo._admin_audit
        def audit(*args,**kwargs):
            result=original(*args,**kwargs);self.control.allowed=False;return result
        with patch.object(self.repo,'_admin_audit',side_effect=audit):
            with self.assertRaises(MediaError):self.adapter.execute(command)
        self.control.allowed=True;self.assertEqual(self.snapshot(),before)
        with self.connect() as c:
            c.execute("CREATE FUNCTION invalidate_local_mapping() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN UPDATE shadify_users SET active=false WHERE auth_account_id='owner-a'; RETURN NEW; END $$")
            c.execute("CREATE TRIGGER invalidate_local_mapping AFTER INSERT ON shadify_app_admin_audit FOR EACH ROW WHEN (NEW.action='local_recover_primary') EXECUTE FUNCTION invalidate_local_mapping()")
        with self.assertRaises(MediaError) as error:self.adapter.execute(command)
        self.assertEqual(error.exception.status,422);self.assertEqual(self.snapshot(),before)

    def test_expired_context_and_deactivated_previous_primary_rules(self):
        command=self.command(mode='apply');before=self.snapshot()
        self.control.cached=self.control.admit(command).model_copy(update={'expires_at':int(time.time())})
        with self.assertRaises(MediaError):self.adapter.execute(command)
        self.assertEqual(self.snapshot(),before);self.control.cached=None
        with self.connect() as c:c.execute("UPDATE shadify_users SET active=false WHERE auth_account_id='admin'")
        result=self.adapter.execute(command)
        self.assertEqual(result['primary_account_id'],'owner-a')

    def test_missing_schema_denies_without_automatic_initialization(self):
        command=self.command()
        with self.connect() as c:c.execute('DROP TABLE shadify_app_admin_delegates,shadify_app_admin')
        with self.assertRaises(psycopg.errors.UndefinedTable):self.adapter.execute(command)
        with self.connect() as c:self.assertIsNone(c.execute("SELECT to_regclass('shadify_app_admin')").fetchone()[0])

    def test_authority_revocation_or_target_deactivation_while_waiting_denies(self):
        for kind in ('authority','mapping'):
            with self.subTest(kind=kind):
                command=self.command(mode='apply');connections=[];original=self.repo.connect
                def connect():
                    c=original();connections.append(c);return c
                with self.connect() as lock,patch.object(self.repo,'connect',side_effect=connect),ThreadPoolExecutor(1) as pool:
                    if kind=='authority':lock.execute('SELECT * FROM shadify_app_admin FOR UPDATE')
                    else:lock.execute("SELECT id FROM shadify_users WHERE auth_account_id='owner-a' FOR UPDATE")
                    future=pool.submit(self.adapter.execute,command)
                    deadline=time.monotonic()+2;waiting=False
                    while time.monotonic()<deadline:
                        if connections:
                            with self.connect() as observer:
                                row=observer.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',
                                    (connections[0].info.backend_pid,)).fetchone()
                            if row and row[0]=='Lock':waiting=True;break
                        time.sleep(.005)
                    self.assertTrue(waiting)
                    if kind=='authority':self.control.allowed=False
                    else:lock.execute("UPDATE shadify_users SET active=false WHERE auth_account_id='owner-a'")
                    lock.commit()
                    with self.assertRaises(MediaError) as error:future.result(5)
                    self.assertEqual(error.exception.status,403 if kind=='authority' else 422)
                self.control.allowed=True
                self.assertEqual(self.admin_revision(),command.expected_admin_revision)
                with self.connect() as c:
                    self.assertEqual(c.execute("SELECT count(*) FROM shadify_app_admin_audit WHERE action='local_recover_primary'").fetchone()[0],0)

    def test_context_expiry_after_audit_rolls_back_without_renewal(self):
        command=self.command(mode='apply');before=self.snapshot();now=int(time.time());clock=[now]
        proof=self.control.admit(command);self.control.cached=proof
        original=self.repo._admin_audit
        def audit(*args,**kwargs):
            result=original(*args,**kwargs);clock[0]=proof.expires_at;return result
        admissions=self.control.admissions
        with patch('shadify_api.local_primary.time.time',side_effect=lambda:clock[0]),             patch.object(self.repo,'_admin_audit',side_effect=audit):
            with self.assertRaises(MediaError) as error:self.adapter.execute(command)
        self.assertEqual(error.exception.status,403)
        self.assertEqual(self.control.admissions,admissions+1)
        self.assertEqual(self.snapshot(),before)

    def test_expiry_during_final_database_check_rolls_back_before_commit(self):
        command=self.command(mode='apply');before=self.snapshot();now=int(time.time());clock=[now]
        proof=self.control.admit(command);self.control.cached=proof
        original=self.adapter._database;calls=[0]
        def database(*args):
            result=original(*args);calls[0]+=1
            if calls[0]==3:clock[0]=proof.expires_at
            return result
        with patch('shadify_api.local_primary.time.time',side_effect=lambda:clock[0]),             patch.object(self.adapter,'_database',side_effect=database):
            with self.assertRaises(MediaError) as error:self.adapter.execute(command)
        self.assertEqual(error.exception.status,403);self.assertEqual(calls[0],3)
        self.assertEqual(self.snapshot(),before)
