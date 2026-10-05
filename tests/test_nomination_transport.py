"""Source-only Node26 UDS/selection and real disposable SQL cancellation."""
import asyncio
import os
import socket
import struct
import threading
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import psycopg
from test_nomination_control import NominationControlCase
from shadify_api.nomination_transport import BoundedReceiptReader, ProtectedNominationComposition
from shadify_api.config import DatabaseConfig


class NominationTransportTests(NominationControlCase):
    def bounded(self, connections):
        def connect():
            c = psycopg.connect(**self.server.connection, connect_timeout=1,
                options='-c search_path='+self.schema+' -c statement_timeout=1000 -c lock_timeout=500')
            connections.append(c)
            return c
        return BoundedReceiptReader(owner=self.nom, connect=connect)

    def test_connection_bounds_are_scoped_to_receipts(self):
        config = DatabaseConfig(host='localhost', dbname='synthetic', user='synthetic',
                                password='synthetic', sslmode='disable')
        with patch('psycopg.connect') as connect:
            config.connect_nomination_receipt()
            self.assertEqual(connect.call_args.kwargs['connect_timeout'],1)
            self.assertEqual(connect.call_args.kwargs['hostaddr'],'127.0.0.1')
            self.assertEqual(connect.call_args.kwargs['options'],'-c statement_timeout=1000 -c lock_timeout=500')
            config.connect()
            self.assertEqual(connect.call_args.kwargs['connect_timeout'],5)
            self.assertEqual(connect.call_args.kwargs['options'],'-c statement_timeout=10000 -c lock_timeout=5000')

    def test_dns_or_multi_endpoint_receipts_deny_before_connect(self):
        for host in ('db.example.invalid','127.0.0.1,127.0.0.2'):
            config=DatabaseConfig(host=host,dbname='synthetic',user='synthetic',
                password='synthetic',sslmode='require')
            with patch('psycopg.connect') as connect:
                with self.assertRaises(ValueError):config.connect_nomination_receipt()
                connect.assert_not_called()

    def test_real_lock_timeout_is_unknown_and_loop_remains_responsive(self):
        cmd=self.command();self.apply_command(cmd);connections=[]
        with self.connect() as lock:
            lock.execute('LOCK shadify_nomination_operations IN ACCESS EXCLUSIVE MODE')
            async def run():
                ticks=0
                task=asyncio.create_task(self.bounded(connections)(self.scope,cmd.operation_id))
                start=time.monotonic()
                while not task.done():
                    ticks+=1;await asyncio.sleep(.01)
                result=await task
                self.assertEqual(result.outcome,'unknown')
                self.assertGreater(ticks,10);self.assertLess(time.monotonic()-start,2)
            asyncio.run(run())
        self.assertTrue(connections and all(c.closed for c in connections))
        self.assertEqual(self.nom.read_committed_operation(self.scope,cmd.operation_id).outcome,'committed')

    def test_cancel_real_query_waits_for_backend_and_connection_cleanup(self):
        cmd=self.command();self.apply_command(cmd);connections=[]
        with self.connect() as lock:
            lock.execute('LOCK shadify_nomination_operations IN ACCESS EXCLUSIVE MODE')
            async def run():
                task=asyncio.create_task(self.bounded(connections)(self.scope,cmd.operation_id))
                deadline=time.monotonic()+2
                pid=None
                while time.monotonic()<deadline:
                    if connections:
                        pid=connections[0].info.backend_pid
                        with self.connect() as observer:
                            row=observer.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',(pid,)).fetchone()
                        if row and row[0]=='Lock':break
                    await asyncio.sleep(.005)
                self.assertIsNotNone(pid);self.assertEqual(row[0],'Lock')
                start=time.monotonic();task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertLess(time.monotonic()-start,.45)
                self.assertTrue(connections[0].closed)
                with self.connect() as observer:
                    self.assertIsNone(observer.execute('SELECT pid FROM pg_stat_activity WHERE pid=%s',(pid,)).fetchone())
            asyncio.run(run())

    def test_cancel_during_connection_fences_late_connection_close(self):
        started=threading.Event();release=threading.Event();connections=[]
        def connect():
            started.set();release.wait(1)
            c=self.connect();connections.append(c);return c
        async def run():
            task=asyncio.create_task(BoundedReceiptReader(owner=self.nom,connect=connect)(self.scope,str(uuid4())))
            await asyncio.to_thread(started.wait,1)
            task.cancel();await asyncio.sleep(.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertTrue(connections[0].closed)
        asyncio.run(run())

    def test_repeated_cancellation_still_fences_cleanup(self):
        started=threading.Event();release=threading.Event();connections=[]
        def connect():
            started.set();release.wait(1)
            c=self.connect();connections.append(c);return c
        async def run():
            task=asyncio.create_task(BoundedReceiptReader(owner=self.nom,connect=connect)(self.scope,str(uuid4())))
            await asyncio.to_thread(started.wait,1)
            task.cancel();await asyncio.sleep(.01);task.cancel();await asyncio.sleep(.01)
            self.assertFalse(task.done());release.set()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertTrue(connections[0].closed)
        asyncio.run(run())

    def test_real_statement_timeout_cancels_sleep_and_closes_connection(self):
        connections=[]
        class SleepingOwner:
            def read_committed_operation(_,scope,operation_id,*,receipt_connect):
                with receipt_connect() as connection:
                    connection.execute('SELECT pg_sleep(10)')
        async def run():
            reader=BoundedReceiptReader(owner=SleepingOwner(),connect=self.bounded(connections).connect)
            start=time.monotonic()
            with self.assertRaises(psycopg.errors.QueryCanceled):await reader(self.scope,str(uuid4()))
            self.assertLess(time.monotonic()-start,2)
            self.assertTrue(connections[0].closed)
        asyncio.run(run())

    def composition(self, tmp):
        from node_agent_package_helper.nomination_contract import Scope, Witness
        from node_agent_package_helper.nomination_reader import InstalledSelection, Denied, serve_connection
        from node_agent_package_helper.nomination_result_transport import ManagementSelection, require_management
        from node_agent_package_helper.nomination_reader import process_commitment
        common=Scope.model_validate(self.scope.model_dump(mode='json'))
        ticks,uids,cwd,_=process_commitment(os.getpid())
        selection=InstalledSelection(scope=common,node_id=str(uuid4()),uid=os.getuid(),pid=os.getpid(),
            start_ticks=ticks,source_directory=cwd,source_commit='a'*40,manifest_sha256='b'*64,
            unit='node-agent-shadify-api.service',protected_revision='c'*32)
        installed=[selection];native=[(selection.node_id,'shadify_api',self.scope.app_instance_id)];peers=[];threads=[]
        management=ManagementSelection(uid=os.getuid(),pid=os.getpid(),start_ticks=ticks,source_directory=cwd)
        proc=Path(tmp)/'proc';p=proc/str(os.getpid());p.mkdir(parents=True)
        (p/'stat').write_text(Path('/proc',str(os.getpid()),'stat').read_text())
        (p/'status').write_text(Path('/proc',str(os.getpid()),'status').read_text())
        (p/'cwd').symlink_to(cwd,target_is_directory=True)
        (p/'cgroup').write_text('0::/system.slice/node-agent.service\n')
        def scope():
            if native[0][1:]!=('shadify_api',self.scope.app_instance_id):raise Denied()
            return self.scope
        class Backend:
            def installed_selection(_,requested):
                if requested!=installed[0].scope:raise Denied()
                return installed[0]
            def require_current(_,expected):
                if installed[0]!=expected:raise Denied()
            def read_verified_witness(_,expected,op):
                body=self.reader.witness.model_dump(mode='json');body['issued_at']=self.actor.issued_at
                return Witness.model_validate(body)
        backend=Backend()
        def connect():
            app,node=socket.socketpair()
            def serve():
                # Same UID kernel peer, original stat/status/cwd; fixture unit.
                app_proc=Path(tmp)/'app-proc';ap=app_proc/str(os.getpid());ap.mkdir(parents=True,exist_ok=True)
                for name in ('stat','status'):(ap/name).write_text((p/name).read_text())
                if not (ap/'cwd').exists():(ap/'cwd').symlink_to(cwd,target_is_directory=True)
                (ap/'cgroup').write_text('0::/system.slice/node-agent-shadify-api.service\n')
                try:serve_connection(node,backend,proc=app_proc)
                except BrokenPipeError:pass  # Client denied peer before receiving.
                finally:node.close()
            thread=threading.Thread(target=serve);threads.append(thread);thread.start();return app
        def peer(fd):
            credentials=struct.unpack('3i',fd.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            require_management(management,credentials,proc=proc);peers.append(credentials)
        composition=ProtectedNominationComposition(connect_descriptor=connect,require_reader_peer=peer,
            read_installed=lambda _:installed[0],management=management,require_control=lambda _:None)
        connections=[];adapter=composition.bind(self.nom,scope,self.bounded(connections).connect,
            native_identity_provider=lambda:native[0])
        self.nom.provider=adapter
        return composition,installed,native,peers,threads,proc,connections

    def test_actual_node_provider_preserves_original_and_denies_native_selection_peer_drift(self):
        from node_agent_package_helper.nomination_reader import Denied
        cmd=self.command();self.apply_command(cmd)
        with TemporaryDirectory() as tmp:
            composition,installed,native,peers,threads,proc,_=self.composition(tmp)
            proof=self.nom.read_verified_witness(self.scope,cmd.operation_id)
            self.assertEqual(proof,self.reader.witness);self.assertGreaterEqual(len(peers),6)
            native[0]=(installed[0].node_id,'shadify_api','wrong')
            with self.assertRaises(Denied):self.nom.require_current(self.scope,proof)
            native[0]=(installed[0].node_id,'shadify_api',self.scope.app_instance_id)
            original=installed[0];installed[0]=replace(original,protected_revision='changed')
            with self.assertRaises(Denied):self.nom.require_current(self.scope,proof)
            installed[0]=original
            (proc/str(os.getpid())/'stat').write_text('invalid')
            with self.assertRaises(Denied):self.nom.require_current(self.scope,proof)
            for thread in threads:thread.join(4);self.assertFalse(thread.is_alive())

    def test_reverse_uds_immutable_receipt_and_descriptor_cleanup(self):
        from node_agent_package_helper.nomination_result_transport import StatusRequest, require_management
        cmd=self.command();self.apply_command(cmd)
        with TemporaryDirectory() as tmp:
            composition,installed,_,_,threads,proc,connections=self.composition(tmp)
            node,app=socket.socketpair()
            async def run():
                from node_agent_package_helper.nomination_result_transport import receive_async,send_async
                request=StatusRequest(protocol_version='node-agent-nomination-result-v1',action='read_operation_result',request_id=str(uuid4()),
                    scope=installed[0].scope,operation_id=cmd.operation_id)
                with patch('node_agent_package_helper.nomination_result_transport.require_management',
                           side_effect=lambda selection,credentials,**_:require_management(selection,credentials,proc=proc)):
                    task=asyncio.create_task(composition.serve_receipt(app))
                    deadline=time.monotonic()+3
                    await send_async(node,request.model_dump(mode='json'),deadline)
                    response=await receive_async(node,deadline);await task
                self.assertEqual(response['result']['outcome'],'committed')
                self.assertEqual(response['result']['receipt']['status'],'pending')
                self.assertEqual(response['result']['receipt']['revision'],1)
                self.assertEqual(app.fileno(),-1)
                self.assertTrue(connections and all(c.closed for c in connections))
            try:asyncio.run(run())
            finally:node.close();app.close()
            for thread in threads:thread.join(4);self.assertFalse(thread.is_alive())

    def test_reverse_descriptor_is_closed_on_cancellation_before_request(self):
        cmd=self.command();self.apply_command(cmd)
        with TemporaryDirectory() as tmp:
            composition,_,_,_,_,proc,_=self.composition(tmp)
            node,app=socket.socketpair()
            async def run():
                from node_agent_package_helper.nomination_result_transport import require_management
                with patch('node_agent_package_helper.nomination_result_transport.require_management',
                           side_effect=lambda selection,credentials,**_:require_management(selection,credentials,proc=proc)):
                    task=asyncio.create_task(composition.serve_receipt(app));await asyncio.sleep(.02);task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                self.assertEqual(app.fileno(),-1)
            try:asyncio.run(run())
            finally:node.close();app.close()

    def test_reverse_cancel_during_sql_fences_query_and_closes_descriptor(self):
        from node_agent_package_helper.nomination_result_transport import StatusRequest, send_async, require_management
        cmd=self.command();self.apply_command(cmd)
        with TemporaryDirectory() as tmp, self.connect() as lock:
            composition,installed,_,_,threads,proc,connections=self.composition(tmp)
            lock.execute('LOCK shadify_nomination_operations IN ACCESS EXCLUSIVE MODE')
            node,app=socket.socketpair()
            async def run():
                with patch('node_agent_package_helper.nomination_result_transport.require_management',
                           side_effect=lambda selection,credentials,**_:require_management(selection,credentials,proc=proc)):
                    task=asyncio.create_task(composition.serve_receipt(app))
                    request=StatusRequest(protocol_version='node-agent-nomination-result-v1',
                        action='read_operation_result',request_id=str(uuid4()),
                        scope=installed[0].scope,operation_id=cmd.operation_id)
                    await send_async(node,request.model_dump(mode='json'),time.monotonic()+3)
                    deadline=time.monotonic()+2;row=None
                    while time.monotonic()<deadline:
                        if connections:
                            pid=connections[0].info.backend_pid
                            with self.connect() as observer:
                                row=observer.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',(pid,)).fetchone()
                            if row and row[0]=='Lock':break
                        await asyncio.sleep(.005)
                    self.assertIsNotNone(row);self.assertEqual(row[0],'Lock')
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                    self.assertTrue(connections[0].closed);self.assertEqual(app.fileno(),-1)
                    with self.connect() as observer:
                        self.assertIsNone(observer.execute('SELECT pid FROM pg_stat_activity WHERE pid=%s',(pid,)).fetchone())
            try:asyncio.run(run())
            finally:node.close();app.close()
            for thread in threads:thread.join(4);self.assertFalse(thread.is_alive())

    def endpoint_descriptor(self):
        # Explicit synthetic approved descriptor; never observe/pin live values.
        import stat
        from node_agent_package_helper.nomination_endpoint import (
            SOCKET,LAUNCHER,MODULE,ReaderUnits,FileCommitment,EndpointSnapshot)
        units=ReaderUnits('node-agent-nomination-reader.socket','node-agent-nomination-reader@.service',
            Path('/etc/systemd/system/node-agent-nomination-reader.socket'),
            Path('/etc/systemd/system/node-agent-nomination-reader@.service'),
            'a'*64,'b'*64,(),True,True,SOCKET,('/usr/bin/python3','-I',str(LAUNCHER)),
            'c'*64,MODULE,Path('/opt/node-agent/releases/source-fixture'),'d'*64,True,'e'*40,'f'*64,'a'*64)
        leaves={SOCKET,LAUNCHER,units.socket_fragment,units.service_fragment,units.release_path}
        paths=set(leaves)
        for path in leaves:paths.update(path.parents)
        files=[]
        for index,path in enumerate(sorted(paths)):
            mode=stat.S_IFDIR|0o755;gid=0
            if path==SOCKET:mode=stat.S_IFSOCK|0o660;gid=1002
            elif path==SOCKET.parent:mode=stat.S_IFDIR|0o750;gid=1002
            elif path in (LAUNCHER,units.socket_fragment,units.service_fragment):mode=stat.S_IFREG|0o644
            files.append(FileCommitment(path,1,index+1,0,gid,mode,0,1,1))
        return EndpointSnapshot(1,23,(0,0,0,0),'systemd',units,tuple(files),(1000,)*4,(1002,))

    def test_endpoint_factory_rejects_declarations_and_nonexact_guards(self):
        from node_agent_package_helper.nomination_endpoint import ProtectedReaderEndpointGuard
        from node_agent_package_helper.nomination_reader import Denied
        expected=self.endpoint_descriptor()
        class DerivedGuard(ProtectedReaderEndpointGuard):pass
        for guard in (None,expected,{'root_uid':0},DerivedGuard(expected=expected,observe=lambda:expected,group_id=1002)):
            with self.assertRaises(Denied):
                ProtectedNominationComposition.from_endpoint(endpoint_guard=guard,
                    read_installed=lambda _:None,management=None,require_control=lambda _:None)

    def test_actual_endpoint_factory_pairs_synthetic_manager_provenance_with_real_node_uds(self):
        from node_agent_package_helper.nomination_endpoint import ProtectedReaderEndpointGuard
        cmd=self.command();self.apply_command(cmd)
        with TemporaryDirectory() as tmp:
            direct,installed,_,_,threads,_,_=self.composition(tmp)
            expected=self.endpoint_descriptor();calls=[]
            def observe():calls.append(True);return expected
            guard=ProtectedReaderEndpointGuard(expected=expected,observe=observe,group_id=1002)
            class SyntheticManagerDescriptor:
                # Wire is a real UDS; only inherited-manager credentials are
                # synthetic. This is not actual root/socketactivation evidence.
                def __init__(self,connection):self.connection=connection
                def __getattr__(self,name):return getattr(self.connection,name)
                def getsockopt(self,level,option,*args):
                    if (level,option)==(socket.SOL_SOCKET,socket.SO_PEERCRED):return struct.pack('3i',1,0,0)
                    return self.connection.getsockopt(level,option,*args)
            def connect():
                guard.current()
                return SyntheticManagerDescriptor(direct.connect_descriptor())
            with patch.object(guard,'connect',side_effect=connect):
                endpoint=ProtectedNominationComposition.from_endpoint(endpoint_guard=guard,
                    read_installed=lambda _:installed[0],management=direct.management,require_control=direct.require_control)
                adapter=endpoint.bind(self.nom,lambda:self.scope,self.connect,
                    native_identity_provider=lambda:(installed[0].node_id,
                        self.scope.app_id,self.scope.app_instance_id))
                self.nom.provider=adapter
                self.assertIs(adapter._client._peer.__self__,guard)
                self.assertIs(adapter._client._connect,guard.connect)
                proof=self.nom.read_verified_witness(self.scope,cmd.operation_id)
                self.assertEqual(proof,self.reader.witness);self.assertGreaterEqual(len(calls),15)
            for thread in threads:thread.join(4);self.assertFalse(thread.is_alive())

    def test_endpoint_factory_denies_real_nonroot_peer_before_sql(self):
        from node_agent_package_helper.nomination_endpoint import ProtectedReaderEndpointGuard
        from node_agent_package_helper.nomination_reader import Denied
        cmd=self.command();self.apply_command(cmd)
        with TemporaryDirectory() as tmp:
            direct,installed,_,_,threads,_,_=self.composition(tmp)
            expected=self.endpoint_descriptor()
            guard=ProtectedReaderEndpointGuard(expected=expected,observe=lambda:expected,group_id=1002)
            with patch.object(guard,'connect',side_effect=direct.connect_descriptor):
                endpoint=ProtectedNominationComposition.from_endpoint(endpoint_guard=guard,
                    read_installed=lambda _:installed[0],management=direct.management,require_control=direct.require_control)
                self.nom.provider=endpoint.bind(self.nom,lambda:self.scope,self.connect,
                    native_identity_provider=lambda:(installed[0].node_id,
                        self.scope.app_id,self.scope.app_instance_id))
                with patch.object(self.nom,'connect',side_effect=AssertionError('SQL forbidden')) as connect:
                    with self.assertRaises(Denied):self.nom.read_verified_witness(self.scope,cmd.operation_id)
                    connect.assert_not_called()
            for thread in threads:thread.join(4);self.assertFalse(thread.is_alive())

    def test_endpoint_factory_ending_snapshot_drift_cannot_be_cached_authority(self):
        from node_agent_package_helper.nomination_endpoint import ProtectedReaderEndpointGuard
        from node_agent_package_helper.nomination_reader import Denied
        cmd=self.command();self.apply_command(cmd)
        with TemporaryDirectory() as tmp:
            direct,installed,_,_,threads,_,_=self.composition(tmp)
            expected=self.endpoint_descriptor();calls=[0]
            changed=replace(expected,units=replace(expected.units,release_digest='0'*64))
            def observe():
                calls[0]+=1
                return expected if calls[0]<=3 else changed
            guard=ProtectedReaderEndpointGuard(expected=expected,observe=observe,group_id=1002)
            class SyntheticManagerDescriptor:
                def __init__(self,connection):self.connection=connection
                def __getattr__(self,name):return getattr(self.connection,name)
                def getsockopt(self,level,option,*args):
                    if (level,option)==(socket.SOL_SOCKET,socket.SO_PEERCRED):return struct.pack('3i',1,0,0)
                    return self.connection.getsockopt(level,option,*args)
            def connect():
                guard.current();return SyntheticManagerDescriptor(direct.connect_descriptor())
            with patch.object(guard,'connect',side_effect=connect):
                endpoint=ProtectedNominationComposition.from_endpoint(endpoint_guard=guard,
                    read_installed=lambda _:installed[0],management=direct.management,require_control=direct.require_control)
                self.nom.provider=endpoint.bind(self.nom,lambda:self.scope,self.connect,
                    native_identity_provider=lambda:(installed[0].node_id,
                        self.scope.app_id,self.scope.app_instance_id))
                with self.assertRaises(Denied):self.nom.require_current(self.scope)
                self.assertEqual(calls[0],4)
            for thread in threads:thread.join(4);self.assertFalse(thread.is_alive())

    def test_wrong_native_node_same_app_instance_denies_initial_and_current_selection(self):
        from node_agent_package_helper.nomination_reader import Denied
        cmd=self.command();self.apply_command(cmd)
        with TemporaryDirectory() as tmp:
            composition,installed,native,_,threads,_,_=self.composition(tmp)
            original=installed[0]
            wrong_node=str(uuid4())
            # Same canonical app/instance/scope, stable installed metadata. Only
            # node differs: it must fail before descriptor exchange or SQL.
            native[0]=(wrong_node,self.scope.app_id,self.scope.app_instance_id)
            with patch.object(composition,'connect_descriptor',side_effect=AssertionError('descriptor forbidden')):
                with patch.object(self.nom,'connect',side_effect=AssertionError('SQL forbidden')) as connect:
                    with self.assertRaises(Denied):self.nom.require_current(self.scope)
                    connect.assert_not_called()
            native[0]=(original.node_id,self.scope.app_id,self.scope.app_instance_id)
            def ending_node_drift(_):
                native[0]=(wrong_node,self.scope.app_id,self.scope.app_instance_id)
                return original
            with patch.object(composition,'read_installed',side_effect=ending_node_drift):
                with self.assertRaises(Denied):self.nom.require_current(self.scope)
            native[0]=(original.node_id,self.scope.app_id,self.scope.app_instance_id)
            initial=ProtectedNominationComposition(connect_descriptor=lambda:(_ for _ in ()).throw(AssertionError('descriptor forbidden')),
                require_reader_peer=composition.require_reader_peer,read_installed=lambda _:replace(original,node_id=wrong_node),
                management=composition.management,require_control=composition.require_control)
            with self.assertRaises(Denied):initial.bind(self.nom,lambda:self.scope,self.connect,
                native_identity_provider=lambda:native[0])
            self.assertIsNone(initial.adapter)
            for thread in threads:thread.join(4);self.assertFalse(thread.is_alive())
