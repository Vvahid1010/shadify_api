"""Paired exact Node543 stream and Shadify SQL; synthetic OS/native observations."""
import asyncio,os,socket,time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from node_agent_package_helper.nomination_reader import Denied
from node_agent_package_helper.nomination_result_transport import CatalogStateClient
import node_agent_package_helper.nomination_result_transport as wire
import test_nomination_transport as transport_fixture
from test_nomination_control import NominationControlCase
from shadify_api.nomination_catalog import CatalogSettings,NominationCatalogCallbacks


class CatalogStreamTests(NominationControlCase):
    bounded=transport_fixture.NominationTransportTests.bounded

    @contextmanager
    def paired(self,*,selected=True):
        with TemporaryDirectory() as tmp:
            composition,installed,native,peers,threads,proc,connections=transport_fixture.NominationTransportTests.composition(self,tmp)
            self.settings=CatalogSettings(scope=self.scope,app_origin='https://shadify.example.invalid')
            callbacks=composition.catalog_callbacks(settings_provider=lambda:self.settings,
                receipt_connect=self.bounded(connections).connect) if selected else None
            app_proc=Path(tmp)/'app-proc';ap=app_proc/str(os.getpid());ap.mkdir(parents=True,exist_ok=True)
            for name in ('stat','status'):(ap/name).write_bytes(Path('/proc',str(os.getpid()),name).read_bytes())
            (ap/'cwd').symlink_to(Path.cwd(),target_is_directory=True)
            (ap/'cgroup').write_text('0::/system.slice/node-agent-shadify-api.service\n')
            tasks=[];descriptors=[];requests=[]
            class Backend:
                def require_current(_,selection):
                    if selection!=installed[0]:raise Denied()
            async def connect(selection):
                if selection!=installed[0]:raise Denied()
                node,app=socket.socketpair();descriptors.extend((node,app))
                tasks.append(asyncio.create_task(composition.serve_receipt(app,catalog_callbacks=callbacks)))
                return node
            client=CatalogStateClient(connect=connect,backend=Backend(),proc=app_proc)
            receive=wire.receive_async;management=wire.require_management
            async def observed(connection,deadline):
                value=await receive(connection,deadline)
                if isinstance(value,dict) and value.get('action')=='read_catalog_state':requests.append(value)
                return value
            with patch.object(wire,'receive_async',side_effect=observed),patch.object(wire,'require_management',
                    side_effect=lambda selection,credentials,**_:management(selection,credentials,proc=proc)):
                try:yield client,installed,callbacks,tasks,descriptors,requests,connections
                finally:
                    for descriptor in descriptors:descriptor.close()
                    for thread in threads:thread.join(4);self.assertFalse(thread.is_alive())

    async def finished(self,tasks,descriptors,connections):
        await asyncio.gather(*tasks)
        self.assertTrue(all(d.fileno()==-1 for d in descriptors))
        self.assertTrue(connections and all(c.closed for c in connections))

    def test_paired_catalog_read_and_distinct_ending_correlation(self):
        with self.paired() as (client,installed,_,tasks,descriptors,requests,connections):
            async def run():
                state=await client.read_owner_state(installed[0])
                self.assertEqual(state.actions,['initialize']);self.assertEqual(state.admin_revision,0)
                self.assertIsNone(await client.require_owner_current(installed[0],state))
                await self.finished(tasks,descriptors,connections)
                self.assertEqual(len(requests),2)
                self.assertNotEqual(requests[0]['request_id'],requests[1]['request_id'])
                self.assertEqual(requests[0]['scope'],installed[0].scope.model_dump(mode='json'))
                self.assertEqual(set(requests[0]),{'protocol_version','action','request_id','scope'})
            asyncio.run(run())

    def test_unselected_callbacks_return_unavailable_without_sql(self):
        with self.paired(selected=False) as (client,installed,_,tasks,descriptors,_,connections):
            async def run():
                with self.assertRaises(Denied):await client.read_owner_state(installed[0])
                await asyncio.gather(*tasks)
                self.assertEqual(connections,[]);self.assertTrue(all(d.fileno()==-1 for d in descriptors))
            asyncio.run(run())

    def test_origin_change_is_rejected_by_fresh_node_ending_read(self):
        with self.paired() as (client,installed,_,tasks,descriptors,requests,connections):
            async def run():
                state=await client.read_owner_state(installed[0])
                self.settings=self.settings.model_copy(update={'app_origin':'https://changed.example.invalid'})
                with self.assertRaises(Denied):await client.require_owner_current(installed[0],state)
                self.assertEqual(len(requests),2)
                await self.finished(tasks,descriptors,connections)
            asyncio.run(run())

    def test_server_ending_guard_primary_race_returns_null_not_stale_success(self):
        original=NominationCatalogCallbacks._guard;calls=[0]
        def guarded(callbacks,*args):
            original(callbacks,*args);calls[0]+=1
            if calls[0]==4:self.repo.bootstrap_primary(self.actor,operator_reference='isolated-stream-final-race')
        with self.paired() as (client,installed,_,tasks,descriptors,_,connections),patch.object(
                NominationCatalogCallbacks,'_guard',guarded):
            async def run():
                with self.assertRaises(Denied):await client.read_owner_state(installed[0])
                await self.finished(tasks,descriptors,connections)
                self.assertEqual(self.primary(),'admin')
            asyncio.run(run())

    def test_stream_cancellation_during_real_sql_releases_both_descriptors_and_sessions(self):
        with self.paired() as (client,installed,_,tasks,descriptors,_,connections),self.connect() as lock:
            lock.execute('LOCK shadify_app_admin IN ACCESS EXCLUSIVE MODE')
            async def run():
                task=asyncio.create_task(client.read_owner_state(installed[0]));deadline=time.monotonic()+2;row=None
                while time.monotonic()<deadline:
                    if connections:
                        with self.connect() as c:row=c.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',
                            (connections[0].info.backend_pid,)).fetchone()
                        if row and row[0]=='Lock':break
                    await asyncio.sleep(.005)
                self.assertEqual(row[0],'Lock');task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                for server in tasks:server.cancel()
                for server in tasks:
                    with self.assertRaises(asyncio.CancelledError):await server
                self.assertTrue(all(d.fileno()==-1 for d in descriptors))
                self.assertTrue(connections and all(c.closed for c in connections))
            asyncio.run(run())
