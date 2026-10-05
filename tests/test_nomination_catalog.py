"""Disposable SQL catalog callbacks; synthetic settings and Node selection only."""
import asyncio,json,time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
from node_agent_package_helper.nomination_contract import Scope as CommonScope,NominationOwnerState
from node_agent_package_helper.nomination_reader import InstalledSelection
from node_agent_package_helper.nomination_result_transport import CatalogStateRequest,CatalogStateResponse
from test_nomination_control import NominationControlCase,TAG
import test_nomination_transport as transport_fixture
from shadify_api.nomination_catalog import CatalogSettings,NominationCatalogCallbacks
from shadify_api.media import MediaError
from shadify_api.nomination_transport import ProtectedNominationComposition


class CatalogTests(NominationControlCase):
    bounded=transport_fixture.NominationTransportTests.bounded
    def setUp(self):
        super().setUp();self.connections=[]
        self.settings=CatalogSettings(scope=self.scope,app_origin='https://shadify.example.invalid')
        self.selection=InstalledSelection(scope=CommonScope.model_validate(self.scope.model_dump(mode='json')),
            node_id=str(uuid4()),uid=1000,pid=2222,start_ticks=1,source_directory=Path('/synthetic/source'),
            source_commit='a'*40,manifest_sha256='b'*64,unit='node-agent-shadify-api.service',protected_revision='fixture')
        self.selected=self.selection
        def require(selection):
            if selection!=self.selected:raise MediaError(409,'Synthetic selection changed')
        def connect():
            c=self.connect();self.connections.append(c);return c
        self.catalog=NominationCatalogCallbacks(owner=self.nom,settings_provider=lambda:self.settings,
            require_selection=require,receipt_connect=connect)
    def read(self):return asyncio.run(self.catalog.read_owner_state(self.selection))
    def current(self,state):return asyncio.run(self.catalog.require_owner_current(self.selection,state))

    def test_authoritative_empty_and_primary_slots_have_exact_actions(self):
        state=self.read();self.assertEqual(state.actions,['initialize']);self.assertEqual(state.admin_revision,0)
        self.assertIsNone(state.primary_account_id);self.assertIsNone(state.nomination)
        self.repo.bootstrap_primary(self.actor,operator_reference='synthetic-only')
        state=self.read();self.assertEqual(state.actions,['recover']);self.assertEqual(state.primary_account_id,'admin')
        self.assertEqual(state.admin_revision,1);self.assertTrue(all(c.closed for c in self.connections))

    def test_pending_revision_expiry_actions_and_redacted_wire_contract(self):
        command=self.command();self.apply_command(command);state=self.read()
        self.assertEqual(state.nomination.id,command.nomination_id);self.assertEqual(state.nomination.revision,1)
        self.assertEqual(state.nomination.expires_at,command.expires_at)
        self.assertEqual(state.actions,['renew','revoke','replace'])
        self.assertEqual(state.validity_limits.maximumMinutes,1440)
        response=CatalogStateResponse(protocol_version='node-agent-nomination-result-v1',request_id=str(uuid4()),state=state)
        raw=response.model_dump_json()
        for secret in (TAG,'synthetic@example.invalid','owner-a',self.actor.session_id,'body_sha256',
                       'email_tags','phone_tags','existing_account_id','approval','password','dsn','witness'):
            self.assertNotIn(secret,raw)
        for key in ('operation_id','contact','token','body','expected','callback_url','account_id'):
            with self.assertRaises(ValueError):CatalogStateRequest.model_validate(dict(
                protocol_version='node-agent-nomination-result-v1',action='read_catalog_state',request_id=str(uuid4()),
                scope=self.selection.scope.model_dump(mode='json'),**{key:'forbidden'}))

    def test_pending_primary_drift_and_expiry_remove_renew(self):
        command=self.command();self.apply_command(command)
        self.repo.bootstrap_primary(self.actor,operator_reference='synthetic-only')
        self.assertEqual(self.read().actions,['revoke','replace'])
        with patch('shadify_api.nomination_catalog.time.time',return_value=command.expires_at):
            state=self.read();self.assertEqual(state.actions,['revoke','replace'])
            self.assertEqual(state.nomination.status,'pending');self.assertEqual(state.nomination.expires_at,command.expires_at)

    def test_current_guard_checks_pending_revision_revoke_replace_and_primary(self):
        command=self.command();self.apply_command(command);before=self.read()
        self.apply_command(self.follow(command,'renew',expires_at=command.expires_at+60))
        with self.assertRaises(MediaError):self.current(before)
        before=self.read();replacement=self.command(operation='replace',nomination_id=command.nomination_id,
            expected_nomination_revision=2,replacement_nomination_id=str(uuid4()))
        self.apply_command(replacement)
        with self.assertRaises(MediaError):self.current(before)
        before=self.read();self.apply_command(self.follow(replacement,'revoke',nomination_id=replacement.replacement_nomination_id))
        with self.assertRaises(MediaError):self.current(before)
        before=self.read();self.repo.bootstrap_primary(self.actor,operator_reference='synthetic-only')
        with self.assertRaises(MediaError):self.current(before)

    def test_scope_origin_limits_selection_and_original_deadline_drift_deny(self):
        original=self.read();self.assertIsNone(self.current(original))
        settings=self.settings
        for changes in ({'app_origin':'https://other.example.invalid'},):
            self.settings=settings.model_copy(update=changes)
            with self.assertRaises(MediaError):self.current(original)
        self.settings=settings
        with patch('shadify_api.nomination_catalog.MAX_LEASE',3600):
            with self.assertRaises(MediaError):self.current(original)
        self.selected=replace(self.selection,protected_revision='changed')
        with self.assertRaises(MediaError):self.read()
        self.selected=self.selection
        other=replace(self.selection,scope=self.selection.scope.model_copy(update={'app_instance_id':str(uuid4())}))
        with self.assertRaises(MediaError):asyncio.run(self.catalog.read_owner_state(other))
        with self.assertRaises(MediaError):self.catalog.read_scope('mizfood_api')
        self.assertEqual(self.catalog.read_scope('shadify_api'),self.selection.scope)
        with patch('shadify_api.nomination_catalog.time.time',return_value=original.observed_at+1):
            self.assertIsNone(self.current(original))
        with patch('shadify_api.nomination_catalog.time.time',return_value=original.expires_at):
            with self.assertRaises(MediaError):self.current(original)
        future=original.model_copy(update={'observed_at':original.expires_at+1,'expires_at':original.expires_at+2})
        with self.assertRaises(MediaError):self.current(future)

    def test_missing_authority_settings_schema_or_ambiguous_slot_deny_without_defaults(self):
        empty=NominationCatalogCallbacks(owner=self.nom)
        with self.assertRaises(MediaError):asyncio.run(empty.read_owner_state(self.selection))
        composition=ProtectedNominationComposition(connect_descriptor=None,require_reader_peer=None,
            read_installed=None,management=None,require_control=None)
        with self.assertRaises(MediaError):composition.catalog_callbacks()
        self.settings=self.settings.model_copy(update={'app_origin':'http://shadify.example.invalid'})
        with self.assertRaises(MediaError):self.read()
        self.assertEqual(self.connections,[])
        self.settings=self.settings.model_copy(update={'app_origin':'https://shadify.example.invalid'})
        with patch.object(self.nom.reader if hasattr(self.nom,'reader') else self.nom.provider,'require_current',side_effect=MediaError(503,'Unavailable')):
            with self.assertRaises(MediaError):self.read()
        with self.connect() as c:c.execute('DELETE FROM shadify_nomination_control')
        with self.assertRaises(MediaError):self.read()
        with self.connect() as c:
            c.execute('INSERT INTO shadify_nomination_control(singleton) VALUES(true)')
            c.execute('DROP TABLE shadify_admin_nomination_v1 CASCADE')
        with self.assertRaises(MediaError):self.read()
        self.assertTrue(all(c.closed for c in self.connections))

    def test_current_read_is_coherent_during_atomic_primary_pending_updates(self):
        command=self.command();self.apply_command(command)
        def update():
            for _ in range(12):
                with self.connect() as c:
                    c.execute("UPDATE shadify_app_admin SET revision=revision+1,primary_user_id=(SELECT id FROM shadify_users WHERE auth_account_id='admin')")
                    c.execute("UPDATE shadify_admin_nomination_v1 SET revision=revision+1,expires_at=expires_at+interval '1 second' WHERE id=%s",(command.nomination_id,))
                time.sleep(.002)
        async def observe():
            for _ in range(20):
                try:state=await self.catalog.read_owner_state(self.selection)
                except MediaError as error:
                    self.assertEqual(error.status,409);continue  # Guard-fence mutation correctly denies.
                self.assertEqual(state.nomination.revision,state.admin_revision+1)
                self.assertEqual(state.primary_account_id,None if state.admin_revision==0 else 'admin')
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(update);asyncio.run(observe());future.result()

    def test_real_query_cancellation_and_timeout_fence_connection_cleanup(self):
        with self.connect() as lock:
            lock.execute('LOCK shadify_app_admin IN ACCESS EXCLUSIVE MODE')
            async def run():
                task=asyncio.create_task(self.catalog.read_owner_state(self.selection));row=None
                deadline=time.monotonic()+2
                while time.monotonic()<deadline:
                    if self.connections:
                        with self.connect() as c:row=c.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',
                            (self.connections[0].info.backend_pid,)).fetchone()
                        if row and row[0]=='Lock':break
                    await asyncio.sleep(.005)
                self.assertEqual(row[0],'Lock');task.cancel();task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertTrue(all(c.closed for c in self.connections))
                start=time.monotonic()
                with self.assertRaises(MediaError):await self.catalog.read_owner_state(self.selection)
                self.assertLess(time.monotonic()-start,2)
                self.assertTrue(all(c.closed for c in self.connections))
            asyncio.run(run())

    def test_cancellation_during_connect_joins_late_cleanup_even_when_repeated(self):
        started=threading.Event();release=threading.Event()
        def connect():
            started.set();release.wait(1)
            c=self.connect();self.connections.append(c);return c
        self.catalog.reader.connect=connect
        async def run():
            task=asyncio.create_task(self.catalog.read_owner_state(self.selection))
            await asyncio.to_thread(started.wait,1)
            task.cancel();await asyncio.sleep(.01);task.cancel();await asyncio.sleep(.01)
            self.assertFalse(task.done());release.set()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertTrue(self.connections and all(c.closed for c in self.connections))
        asyncio.run(run())

    def test_observation_deadline_cannot_be_reset_after_sql(self):
        clock=[int(time.time())];calls=[0]
        def settings():
            calls[0]+=1
            if calls[0]==3:clock[0]+=31
            return self.settings
        self.catalog.settings_provider=settings
        with patch('shadify_api.nomination_catalog.time.time',side_effect=lambda:clock[0]):
            with self.assertRaises(MediaError):self.read()
        self.assertTrue(self.connections and all(c.closed for c in self.connections))

    def test_ambiguous_primary_or_unrepresentable_stored_nomination_denies(self):
        with self.connect() as c:c.execute('UPDATE shadify_app_admin SET revision=1')
        with self.assertRaises(MediaError):self.read()
        with self.connect() as c:c.execute('UPDATE shadify_app_admin SET revision=0')
        command=self.command();self.apply_command(command)
        with self.connect() as c:
            c.execute("UPDATE shadify_admin_nomination_v1 SET state='blocked',revision=revision+1 WHERE id=%s",(command.nomination_id,))
        with self.assertRaises(MediaError):self.read()

    def test_catalog_reads_are_read_only_without_audit_or_state_effects(self):
        command=self.command();self.apply_command(command)
        with self.connect() as c:
            before=tuple(c.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in (
                'shadify_users','shadify_app_admin_audit','shadify_nomination_operations'))
        state=self.read()
        self.assertIsNone(self.current(state))
        with self.connect() as c:
            after=tuple(c.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in (
                'shadify_users','shadify_app_admin_audit','shadify_nomination_operations'))
            row=c.execute('SELECT revision,state,expires_at FROM shadify_admin_nomination_v1 WHERE id=%s',(command.nomination_id,)).fetchone()
        self.assertEqual(after,before);self.assertEqual(row[0:2],(1,'pending'))
        self.assertEqual(int(row[2].timestamp()),command.expires_at)

    def test_existing_guarded_composition_exports_callbacks_without_new_wire_dispatch(self):
        with TemporaryDirectory() as tmp:
            composition,installed,native,peers,threads,_,connections=transport_fixture.NominationTransportTests.composition(self,tmp)
            try:
                callbacks=composition.catalog_callbacks(settings_provider=lambda:self.settings,
                    receipt_connect=self.bounded(connections).connect)
                self.assertEqual(callbacks.read_scope('shadify_api'),installed[0].scope)
                state=asyncio.run(callbacks.read_owner_state(installed[0]))
                self.assertEqual(state.actions,['initialize']);self.assertGreater(len(peers),0)
                self.assertIsNone(asyncio.run(callbacks.require_owner_current(installed[0],state)))
                native[0]=(str(uuid4()),'shadify_api',self.scope.app_instance_id)
                with self.assertRaises(MediaError):asyncio.run(callbacks.read_owner_state(installed[0]))
                self.assertTrue(connections and all(c.closed for c in connections))
            finally:
                for thread in threads:thread.join(4);self.assertFalse(thread.is_alive())


    def test_primary_change_during_last_blocking_guard_denies_current_and_read(self):
        for entry in ('read','current'):
            with self.subTest(entry=entry):
                # Reset only this isolated fixture; app primary cannot be removed.
                if entry=='current':
                    with self.connect() as c:
                        c.execute('DROP TABLE shadify_app_admin_audit,shadify_app_admin_delegates,shadify_app_admin')
                        c.execute('DROP FUNCTION protect_shadify_primary()')
                    self.apply('006_app_admin.sql')
                expected=self.read();entered=threading.Event();release=threading.Event();calls=[0]
                original=self.catalog._guard
                def guard(*args):
                    original(*args);calls[0]+=1
                    if calls[0]==2:
                        entered.set();self.assertTrue(release.wait(2))
                with patch.object(self.catalog,'_guard',side_effect=guard),ThreadPoolExecutor(1) as pool:
                    task=pool.submit(self.current,expected) if entry=='current' else pool.submit(self.read)
                    self.assertTrue(entered.wait(2))
                    self.repo.bootstrap_primary(self.actor,operator_reference='isolated-final-guard-race')
                    release.set()
                    with self.assertRaises(MediaError) as error:task.result(3)
                    self.assertEqual(error.exception.status,409)
                self.assertEqual(self.primary(),'admin')
                self.assertTrue(all(c.closed for c in self.connections))

    def test_pending_revision_change_during_last_blocking_guard_denies_current_and_read(self):
        command=self.command();self.apply_command(command)
        for entry,revision in (('read',1),('current',2)):
            with self.subTest(entry=entry):
                expected=self.read();entered=threading.Event();release=threading.Event();calls=[0]
                original=self.catalog._guard
                def guard(*args):
                    original(*args);calls[0]+=1
                    if calls[0]==2:
                        entered.set();self.assertTrue(release.wait(2))
                with patch.object(self.catalog,'_guard',side_effect=guard),ThreadPoolExecutor(1) as pool:
                    task=pool.submit(self.current,expected) if entry=='current' else pool.submit(self.read)
                    self.assertTrue(entered.wait(2))
                    self.apply_command(self.follow(command,'renew',revision=revision,
                        expires_at=command.expires_at+60*revision))
                    release.set()
                    with self.assertRaises(MediaError) as error:task.result(3)
                    self.assertEqual(error.exception.status,409)
                self.assertEqual(self.row(command.nomination_id)['revision'],revision+1)
                self.assertTrue(all(c.closed for c in self.connections))
