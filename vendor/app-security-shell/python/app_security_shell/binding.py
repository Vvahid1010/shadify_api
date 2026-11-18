"""App-neutral, destination-bound composition of the existing native lifecycle."""
from __future__ import annotations
import asyncio
import copy
import hashlib
from dataclasses import dataclass
import json
import os
from threading import Condition, RLock
import time
from types import MappingProxyType

from .routing import normalize_base, validate_catalog, normalized_catalog
from .transport import Client, SyncClient, TransportFailure


@dataclass(frozen=True)
class Snapshot:
    native: object
    destinations: object
    connections: object
    identity: tuple
    fingerprint: bytes


class _Selected:
    def __init__(self,owner,snapshot):self.owner,self.snapshot=owner,snapshot
    def acquire(self):
        with self.owner._lock:
            if self.owner._active is not self.snapshot:raise ValueError('binding_changed')
            return self.owner.acquire()


class AppSecurityBinding:
    def __init__(self, *, snapshot_provider, directory_provider, receiver_catalog,
                 http_client_provider=None, replay_factory=None, clock=time.time):
        catalog=normalized_catalog(receiver_catalog)
        validate_catalog(catalog)
        self._catalog=catalog
        self._provider,self._directory=snapshot_provider,directory_provider
        self._http_provider,self._replay_factory=http_client_provider,replay_factory
        self.clock=clock
        self._pid=os.getpid()
        self._lock=RLock();self._condition=Condition(self._lock)
        self._preparation=RLock()
        self.registry=self.jobs=None
        self._active=self._prepared=None
        self._identity=None;self._bindings={}
        self._closing=False;self._count=0;self._streams=0
        self._ingress=self._egress=None
        self._clock_bounds=self._storage_bounds=lambda:False
        self._replay=self._replay_close=self._http=None

    @property
    def receiver_catalog(self):return copy.deepcopy(self._catalog)

    def _process(self):
        if self._pid!=os.getpid():raise ValueError('binding_process_changed')

    def _resources(self):
        self._process()
        if self._closing:raise ValueError('binding_draining')
        if self.registry is None:
            from .lifecycle import ContextRegistry
            from .jobs import NativeJobs
            self.registry,self.jobs=ContextRegistry(),NativeJobs()

    def install_integration(self, *, trusted_ingress=None, trusted_egress=None,
                            clock_bounds=None, storage_bounds=None):
        for hook in (trusted_ingress,trusted_egress,clock_bounds,storage_bounds):
            if hook is not None and not callable(hook):raise ValueError('invalid_integration')
        with self._lock:
            self._process()
            if self._closing:raise ValueError('binding_draining')
            self._ingress,self._egress=trusted_ingress,trusted_egress
            self._clock_bounds=clock_bounds or (lambda:False)
            self._storage_bounds=storage_bounds or (lambda:False)
            if self._replay is not None:self._replay.invalidate()

    def trusted_ingress(self,scope):return self._ingress(scope) if self._ingress else None
    def trusted_egress(self,*args):return self._egress(*args) if self._egress else None

    def prepare(self, projection=None):
        # This worker-only lock never blocks enter/leave/drain on an event loop.
        with self._preparation:
            with self._lock:self._resources()
            projection=copy.deepcopy(self._provider() if projection is None else projection)
            if not isinstance(projection,dict) or set(projection)!={'app_security','app_security_binding'}:raise ValueError('invalid_projection')
            cfg,binding=projection['app_security'],projection['app_security_binding']
            if not isinstance(cfg,dict) or not isinstance(binding,dict) or set(binding)!={'schema_version','destinations','outbound_endpoints'}:
                raise ValueError('invalid_projection')
            if type(binding['schema_version']) is not int or binding['schema_version']!=1:raise ValueError('invalid_binding_schema')
            remote=normalized_catalog(binding['outbound_endpoints']);binding['outbound_endpoints']=remote
            local=cfg['local'];identity=tuple(local[k] for k in ('node_id','app_id','app_instance_id'))
            if self._identity is not None and identity!=self._identity:raise ValueError('binding_identity_changed')
            if any(e['receiver_app']!=local['app_id'] for e in self._catalog) or any(e['receiver_app']==local['app_id'] for e in remote):
                raise ValueError('catalog_owner_mismatch')
            catalog=self._catalog+remote;validate_catalog(catalog)
            records=binding['destinations']
            if not isinstance(records,list) or len(records)>64:raise ValueError('invalid_destinations')
            connections={c['connection_id']:c for c in cfg['connections']}
            outgoing={k for k,c in connections.items() if c['sender']['identity']==local}
            destinations={}
            for row in records:
                if not isinstance(row,dict) or set(row)!={'connection_id','base_url'} or not isinstance(row['connection_id'],str):raise ValueError('invalid_destination')
                key=row['connection_id']
                if key in destinations or key not in outgoing:raise ValueError('invalid_destination')
                destinations[key]=normalize_base(row['base_url'])
            if set(destinations)!=outgoing:raise ValueError('missing_destination')
            for key,base in destinations.items():
                old=self._bindings.get(key)
                if old and connections[key]['policy_revision']<=old[0] and base!=old[1]:raise ValueError('binding_revision_conflict')
            raw=json.dumps(cfg,separators=(',',':'),allow_nan=False).encode()
            cat=json.dumps(catalog,separators=(',',':'),allow_nan=False).encode()
            fingerprint=json.dumps([cfg,binding,self._catalog],sort_keys=True,separators=(',',':'),allow_nan=False).encode()
            if len(raw)>1048576 or len(cat)>1048576 or len(fingerprint)>2097152:raise ValueError('projection_limit')
            with self._lock:
                if self._closing:raise ValueError('binding_draining')
                if self._active is not None and fingerprint==self._active.fingerprint:
                    self.registry.acquire()
                    self._prepared=self._active
                    return self._prepared
            native=self.registry.prepare(str(self._directory()),raw,cat)
            candidate=Snapshot(native,MappingProxyType(destinations),MappingProxyType({k:(c['policy_revision'],c['recipient']['identity']['app_id']) for k,c in connections.items()}),identity,fingerprint)
            with self._lock:
                if self._closing:raise ValueError('binding_draining')
                self._prepared=candidate
                return candidate

    def install(self,candidate, *, overlap=()):
        with self._lock:
            self._process()
            if self._closing or candidate is not self._prepared:raise ValueError('unprepared_binding')
            if self._identity is not None and candidate.identity!=self._identity:raise ValueError('binding_identity_changed')
            for key,base in candidate.destinations.items():
                old=self._bindings.get(key)
                if old and candidate.connections[key][0]<=old[0] and base!=old[1]:raise ValueError('binding_revision_conflict')
            if candidate is self._active:
                self.registry.acquire();self._prepared=None
                return
            self.registry.install(candidate.native, overlap=overlap)
            self._identity=candidate.identity
            self._active=candidate;self._prepared=None
            self._bindings.update({k:(candidate.connections[k][0],v) for k,v in candidate.destinations.items()})

    def checkpoint(self):
        with self._lock:
            self._process()
            if self._closing or self.registry is None:
                raise ValueError('checkpoint_unavailable')
            state = self.registry.checkpoint()
            return {
                'schema_version': 1,
                'identity': list(self._identity),
                'projection_sha256': None if state['active'] is None else hashlib.sha256(self._active.fingerprint).hexdigest(),
                'destinations': [[c, revision, base] for c, (revision, base) in sorted(self._bindings.items())],
                'registry': state,
            }

    def restore(self, candidate, *, checkpoint, overlaps):
        # Startup-only. The controller, not an app cache, supplies this bound
        # checkpoint with the approved current and admitted-old projections.
        with self._lock:
            self._resources()
            if self._closing or self._active is not None or candidate is not self._prepared:
                raise ValueError('restore_requires_fresh_binding')
            try:
                if type(checkpoint) is not dict or set(checkpoint) != {
                    'schema_version', 'identity', 'projection_sha256', 'destinations', 'registry'
                } or type(checkpoint['schema_version']) is not int or checkpoint['schema_version'] != 1:
                    raise ValueError('binding_restore_shape')
                identity = checkpoint['identity']
                if type(identity) is not list or len(identity) != 3 or any(type(x) is not str or not 1 <= len(x) <= 128 for x in identity):
                    raise ValueError('binding_restore_identity')
                if identity[1:] != checkpoint['registry']['identity'] or (candidate is not None and tuple(identity) != candidate.identity):
                    raise ValueError('binding_restore_identity')
                expected = None if candidate is None else hashlib.sha256(candidate.fingerprint).hexdigest()
                if checkpoint['projection_sha256'] != expected:
                    raise ValueError('binding_restore_projection')
                rows = checkpoint['destinations']
                if type(rows) is not list or len(rows) > 4096:
                    raise ValueError('binding_restore_destinations')
                bindings = {}
                for row in rows:
                    if (type(row) is not list or len(row) != 3 or type(row[0]) is not str
                            or not 1 <= len(row[0]) <= 128 or row[0] in bindings
                            or type(row[1]) is not int or not 1 <= row[1] < (1 << 64)
                            or type(row[2]) is not str or normalize_base(row[2]) != row[2]):
                        raise ValueError('binding_restore_destinations')
                    bindings[row[0]] = (row[1], row[2])
                if any(bindings.get(c) != (candidate.connections[c][0], base)
                       for c, base in (() if candidate is None else candidate.destinations.items())):
                    raise ValueError('binding_restore_destinations')
                if type(overlaps) is not dict or len(overlaps) > 64 or any(type(v) is not Snapshot for v in overlaps.values()):
                    raise ValueError('binding_restore_overlap')
                self.registry.restore(None if candidate is None else candidate.native, checkpoint=checkpoint['registry'],
                                      overlaps={c: v.native for c, v in overlaps.items()})
            except Exception:
                self.registry.drain()
                self._prepared = None
                raise
            self._identity = tuple(identity)
            self._active, self._prepared = candidate, None
            self._bindings = bindings

    def _initialize(self):
        with self._preparation:
            with self._lock:self._resources()
            if self.registry.status()['state']=='Not configured':self.install(self.prepare())
            return self.acquire()

    async def ready(self):
        with self._lock:self._resources();jobs=self.jobs
        return await jobs.run(self._initialize)

    def acquire(self):
        with self._lock:
            self._process()
            if self._closing or self._active is None:raise ValueError('binding_unavailable')
            return self.registry.acquire()

    def acquire_inbound(self, wire):
        with self._lock:
            self.acquire()
            return self.registry.acquire_inbound(wire)

    def acquire_plain(self, connection, metadata):
        with self._lock:
            self.acquire()
            return self.registry.acquire_plain(connection, metadata)

    def admit_inbound(self, selected, connection):
        with self._lock:
            self.acquire()
            self.registry.admit_inbound(selected, connection)

    def overlap_status(self, connection):
        with self._lock:
            self.acquire()
            return self.registry.overlap_status(connection)

    def mark_cutover(self, connection, *, expected_old, expected_current):
        with self._lock:
            self.acquire()
            self.registry.mark_cutover(connection, expected_old=expected_old, expected_current=expected_current)

    def retire_overlap(self, connection, *, expected_old, expected_current):
        with self._lock:
            self.acquire()
            self.registry.retire_overlap(connection, expected_old=expected_old, expected_current=expected_current)

    def receiver_routes(self):
        with self._lock:
            routes = {(e['method'], e['path']) for e in self._catalog}
            if self.registry is not None:
                routes.update(self.registry.receiver_routes())
            return tuple(sorted(routes))

    def status(self):
        with self._lock:
            self._process()
            return self.registry.status() if self.registry else {'state':'Draining' if self._closing else 'Not configured'}

    def revoke(self,instance,generation):
        with self._lock:
            self._resources();self.registry.revoke(instance,generation)

    def enter(self):
        with self._condition:
            self._process()
            if self._closing:raise ValueError('binding_draining')
            self._count+=1

    def leave(self):
        with self._condition:
            self._count-=1;self._condition.notify_all()

    def drain(self):
        with self._lock:
            self._process();self._closing=True
            if self.registry:self.registry.drain()

    async def close(self):
        self.drain()
        def wait():
            with self._condition:self._condition.wait_for(lambda:self._count==0)
        await asyncio.to_thread(wait)
        if self.jobs:await self.jobs.close()
        if self._replay_close:
            close,self._replay_close=self._replay_close,None
            await close()
        if self._http is not None:
            client,self._http=self._http,None
            await client.aclose()

    async def reserve(self,digest,expires_at,now):
        if self._clock_bounds() is not True or self._storage_bounds() is not True:
            if self._replay:self._replay.invalidate()
            raise ValueError('replay_bounds_unavailable')
        if self._replay is None:
            with self._lock:
                if self._replay is None:
                    if self._replay_factory is None:raise ValueError('replay_unavailable')
                    instance=self.acquire().status()[1]
                    self._replay,self._replay_close=self._replay_factory(instance,lambda:self._clock_bounds(),lambda:self._storage_bounds())
        await self._replay.reserve(digest,expires_at,now)

    def _select(self,connection,expected_base,recipient_app):
        with self._lock:
            self.acquire();snapshot=self._active
            base=snapshot.destinations[connection]
            if expected_base is not None and normalize_base(expected_base)!=base:raise ValueError('destination_mismatch')
            if recipient_app is not None and snapshot.connections[connection][1]!=recipient_app:raise ValueError('recipient_mismatch')
            return _Selected(self,snapshot),base

    async def request(self,connection_id,method,path,*,query=b'',body=b'',headers=None,timeout=30,http_client=None,expected_base=None,recipient_app=None):
        if type(timeout) not in (int,float) or not 0<timeout<=3600:raise TransportFailure('not_sent')
        entered=False
        try:
            self.enter();entered=True
            deadline=time.monotonic()+timeout
            async with asyncio.timeout(timeout):
                await self.ready()
                source,base=self._select(connection_id,expected_base,recipient_app)
                if http_client is None:
                    if self._http_provider:http_client=await self._http_provider()
                    else:
                        import httpx
                        if self._http is None:self._http=httpx.AsyncClient(trust_env=False)
                        http_client=self._http
            return await Client(source,http_client,jobs=self.jobs,clock=self.clock,trusted_egress=self.trusted_egress).request(connection=connection_id,base_url=base,method=method,path=path,query=query,body=body,headers=headers,timeout=deadline-time.monotonic())
        except asyncio.CancelledError:raise
        except TransportFailure:raise
        except TimeoutError:raise TransportFailure('not_sent', reason='timeout') from None
        except Exception:raise TransportFailure('not_sent') from None
        finally:
            if entered:self.leave()

    async def post_json(self,connection_id,path,json_body,**kwargs):
        from .transport import _json_input
        try:body,headers=_json_input(json_body,kwargs.pop('headers',None))
        except Exception:raise TransportFailure('not_sent') from None
        return await self.request(connection_id,'POST',path,body=body,headers=headers,**kwargs)

    def request_sync(self,connection_id,method,path,*,query=b'',body=b'',headers=None,timeout=15,http_client=None,expected_base=None,recipient_app=None):
        if type(timeout) not in (int,float) or not 0<timeout<=3600:raise TransportFailure('not_sent')
        entered=False;owned=None
        try:
            self.enter();entered=True
            deadline=time.monotonic()+timeout
            with self._lock:self._resources();jobs=self.jobs
            jobs.run_sync(self._initialize,timeout=timeout)
            source,base=self._select(connection_id,expected_base,recipient_app)
            if http_client is None:
                import httpx
                owned=http_client=httpx.Client(trust_env=False)
            return SyncClient(source,http_client,jobs=jobs,clock=self.clock,trusted_egress=self.trusted_egress).request(connection=connection_id,base_url=base,method=method,path=path,query=query,body=body,headers=headers,timeout=deadline-time.monotonic())
        except TransportFailure:raise
        except TimeoutError:raise TransportFailure('not_sent', reason='timeout') from None
        except Exception:raise TransportFailure('not_sent') from None
        finally:
            if owned:owned.close()
            if entered:self.leave()

    def post_json_sync(self,connection_id,path,json_body,**kwargs):
        from .transport import _json_input
        try:body,headers=_json_input(json_body,kwargs.pop('headers',None))
        except Exception:raise TransportFailure('not_sent') from None
        return self.request_sync(connection_id,'POST',path,body=body,headers=headers,**kwargs)


    async def open_stream(self, connection_id, path, *, query=b'', headers=None,
                          deadline, http_client=None, expected_base=None, recipient_app=None,
                          open_timeout=30, idle_timeout=60):
        from .streaming import MAX_STREAMS, open_stream
        released = False
        entered = False
        def release():
            nonlocal released
            if entered and not released:
                released=True
                with self._lock: self._streams-=1
                self.leave()
        try:
            for value in (open_timeout, idle_timeout):
                if type(value) not in (int, float) or not 0 < value <= 3600: raise ValueError('invalid_stream_timeout')
            opening_deadline = min(deadline, time.monotonic() + open_timeout)
            with self._lock:
                if self._streams>=MAX_STREAMS:raise ValueError('stream_capacity')
                self.enter();self._streams+=1;entered=True
            remaining=deadline-time.monotonic()
            if not 0<remaining<=3600:raise ValueError('invalid_deadline')
            async with asyncio.timeout_at(opening_deadline):
                await self.ready()
                source,base=self._select(connection_id,expected_base,recipient_app)
                if http_client is None:
                    if self._http_provider:http_client=await self._http_provider()
                    else:
                        import httpx
                        if self._http is None:self._http=httpx.AsyncClient(trust_env=False)
                        http_client=self._http
            client=Client(source,http_client,jobs=self.jobs,clock=self.clock,trusted_egress=self.trusted_egress)
            return await open_stream(client,connection=connection_id,base_url=base,path=path,
                                     query=query,headers=headers,deadline=deadline,release=release,
                                     open_timeout=opening_deadline-time.monotonic(),idle_timeout=idle_timeout)
        except asyncio.CancelledError:
            release();raise
        except TransportFailure:
            release();raise
        except Exception:
            release();raise TransportFailure('not_sent') from None


    def route_metadata(self, connection_id, method, path, *, recipient_app=None,
                       include_transport_binding=False):
        """Public route and optional native channel tuple from one snapshot."""
        if type(include_transport_binding) is not bool:
            raise ValueError('invalid_transport_binding_request')
        source, base = self._select(connection_id, None, recipient_app)
        native = source.acquire()
        endpoint, encrypted = native.route(connection_id, method, path)
        declaration = json.loads(native.endpoint_metadata(endpoint))
        result = {'base_url': base, 'endpoint': declaration, 'encrypted': encrypted}
        if include_transport_binding:
            result['transport_binding'] = native.transport_binding(connection_id)
        return result
