"""Compiled OFF stream admission with an incremental bounded ASGI transport."""
import asyncio
import copy
import gzip
import time

import httpx
import pytest

from app_security_shell._native import NativeContext
from app_security_shell.jobs import NativeJobs
from app_security_shell.transport import Client, Middleware, TransportFailure, TrustedPeer, POLICY_HEADER
from app_security_shell.streaming import open_stream
from app_security_shell.lifecycle import ContextRegistry
from test_pair import pair, canonical, identity, fixture_egress
from test_rotation_overlap import rotated


def contexts(pair, *, off=True, stream=True, profile=True):
    da, db, ha, hb, common, catalog = pair[0].inputs
    common, catalog = copy.deepcopy(common), copy.deepcopy(catalog)
    catalog[0].update(method='GET', response_stream=stream, proxy_headers=profile)
    if profile: catalog[0]['headers'] += ['set-cookie', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto']
    if off: common['endpoint_overrides']=[dict(connection_id='pair',endpoint_id='echo',extra_encryption=False)]
    def build(directory, material, digest):
        cfg=common|dict(local=identity(material),credential_snapshot_sha256=digest)
        return NativeContext(str(directory),canonical(cfg),canonical(catalog))
    return build(da,pair[1],ha),build(db,pair[2],hb)


class IncrementalTransport(httpx.AsyncBaseTransport):
    """One queued chunk proves first-byte delivery without whole-body buffering."""
    def __init__(self, app): self.app=app;self.calls=0;self.closed=0
    async def handle_async_request(self, request):
        self.calls+=1
        queue=asyncio.Queue(maxsize=1); head=asyncio.get_running_loop().create_future()
        disconnect=asyncio.Event();delivered=False
        async def receive():
            nonlocal delivered
            if not delivered:
                delivered=True
                return {'type':'http.request','body':await request.aread(),'more_body':False}
            await disconnect.wait();return {'type':'http.disconnect'}
        async def send(message):
            if message['type']=='http.response.start':head.set_result(message)
            else:
                if message.get('body'):await queue.put(message['body'])
                if not message.get('more_body',False):await queue.put(None)
        scope={'type':'http','asgi':{'version':'3.0'},'method':request.method,'path':request.url.path,
               'raw_path':request.url.raw_path.split(b'?')[0],'query_string':request.url.query,
               'headers':request.headers.raw,'root_path':'','scheme':'http','server':('fixture',80), 'client':('fixture',1)}
        async def run():
            try:await self.app(scope,receive,send)
            except BaseException as exc:
                if not head.done():head.set_exception(exc)
                else:await queue.put(exc)
        task=asyncio.create_task(run());outer=self
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    item=await queue.get()
                    if item is None:break
                    if isinstance(item,BaseException):raise item
                    yield item
            async def aclose(self):
                outer.closed+=1;disconnect.set();task.cancel()
                await asyncio.gather(task,return_exceptions=True)
        try: start=await head
        except BaseException:
            task.cancel();await asyncio.gather(task,return_exceptions=True);raise
        return httpx.Response(start['status'],headers=start['headers'],stream=Body())


def run_pair(pair, app, exercise, *, lifetime=1, off=True, stream=True, handler_timeout=None, idle_timeout=60):
    a,b=contexts(pair,off=off,stream=stream)
    async def run():
        jobs=NativeJobs()
        peer=TrustedPeer('pair',pair[1]['node_id'],pair[1]['app_instance_id'])
        middleware=Middleware(app,b,None,jobs=jobs,trusted_ingress=lambda scope:peer,
                              handler_timeout=lifetime if handler_timeout is None else handler_timeout,
                              stream_timeout=lifetime, stream_idle_timeout=idle_timeout)
        transport=IncrementalTransport(middleware)
        try:
            async with httpx.AsyncClient(transport=transport) as http:
                client=Client(a,http,jobs=jobs,trusted_egress=fixture_egress(a))
                await exercise(client,transport)
        finally:await jobs.close()
    asyncio.run(run())


def test_heartbeat_and_delayed_bar_outlive_opening_budget(pair):
    closed = []
    async def app(scope, receive, send):
        try:
            await send(dict(type='http.response.start', status=200, headers=[(b'content-type', b'text/event-stream')]))
            for _ in range(8):
                await send(dict(type='http.response.body', body=b': keepalive\n\n', more_body=True))
                await asyncio.sleep(.04)
            await send(dict(type='http.response.body', body=b'event: bar\ndata: {}\n\n'))
        finally:
            closed.append(True)
    async def exercise(client, transport):
        response = await open_stream(client, connection='pair', base_url='http://fixture', path='/echo',
                                     deadline=time.monotonic()+2, open_timeout=.2, idle_timeout=.15)
        received = b''.join([chunk async for chunk in response.aiter_bytes()])
        assert received.count(b': keepalive') == 8 and b'event: bar' in received
        assert closed == [True] and transport.closed == 1
    run_pair(pair, app, exercise, lifetime=2, handler_timeout=.2, idle_timeout=.15)


def test_old_off_stream_survives_overlap_until_retirement(pair, tmp_path):
    pair[0].inputs[5][0].update(method='GET', response_stream=True)
    sender, receiver = pair[0](off=True, registries=True)
    old_sender = sender.acquire()
    _, inputs = rotated(pair, tmp_path, mode=False)
    now = [1000.0]
    receiver._clock = lambda: now[0]
    receiver.install(receiver.prepare(*inputs), overlap=('pair',))
    closed = []

    async def app(scope, receive, send):
        try:
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body', 'body': b'data: old\n\n',
                        'more_body': True})
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    async def run():
        jobs = NativeJobs()
        peer = TrustedPeer('pair', pair[1]['node_id'], pair[1]['app_instance_id'])
        middleware = Middleware(
            app, receiver, None, jobs=jobs, trusted_ingress=lambda scope: peer,
            handler_timeout=.5, stream_timeout=2, stream_idle_timeout=1,
        )
        transport = IncrementalTransport(middleware)
        try:
            async with httpx.AsyncClient(transport=transport) as http:
                client = Client(
                    old_sender, http, jobs=jobs,
                    trusted_egress=fixture_egress(old_sender),
                )
                response = await open_stream(
                    client, connection='pair', base_url='http://fixture', path='/echo',
                    deadline=time.monotonic() + 2, idle_timeout=1,
                )
                iterator = response.aiter_bytes()
                assert await anext(iterator) == b'data: old\n\n'
                proof = receiver.overlap_status('pair')
                receiver.mark_cutover('pair', **proof)
                now[0] += 211
                receiver.retire_overlap('pair', **proof)
                with pytest.raises(TransportFailure):
                    await asyncio.wait_for(anext(iterator), .8)
                await response.aclose()
                assert closed == [True]
                assert transport.closed == 1 and middleware._streams == 0
                assert client._stream_count == 0
        finally:
            await jobs.close()

    asyncio.run(run())


def test_receiver_idle_interrupts_blocked_stream_send_and_releases(pair):
    blocked = asyncio.Event()
    closed = []

    async def app(scope, receive, send):
        try:
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body', 'body': b'data: first\n\n',
                        'more_body': True})
            blocked.set()
            await send({'type': 'http.response.body', 'body': b'data: blocked\n\n',
                        'more_body': True})
        finally:
            closed.append(True)

    async def exercise(client, transport):
        response = await open_stream(
            client, connection='pair', base_url='http://fixture', path='/echo',
            deadline=time.monotonic() + 2, idle_timeout=1,
        )
        await asyncio.wait_for(blocked.wait(), .5)
        for _ in range(20):
            if closed:
                break
            await asyncio.sleep(.02)
        assert closed == [True]
        await response.aclose()
        assert transport.closed == 1

    run_pair(pair, app, exercise, lifetime=2, idle_timeout=.15)


@pytest.mark.parametrize('field', ['stream_timeout', 'stream_idle_timeout'])
@pytest.mark.parametrize('value', [0, -1, True, float('nan'), float('inf'), 3601])
def test_receiver_stream_budgets_fail_closed(field, value):
    async def app(scope, receive, send):
        raise AssertionError('must not dispatch')

    with pytest.raises(ValueError, match='invalid_stream_timeout'):
        Middleware(app, object(), None, **{field: value})


@pytest.mark.parametrize('side', ['sender', 'receiver'])
def test_idle_stream_closes_before_authority_expiry(pair, side):
    closed = []
    async def app(scope, receive, send):
        try:
            await send(dict(type='http.response.start', status=200, headers=[(b'content-type', b'text/event-stream')]))
            await send(dict(type='http.response.body', body=b': keepalive\n\n', more_body=True))
            await asyncio.Event().wait()
        finally:
            closed.append(True)
    async def exercise(client, transport):
        response = await open_stream(client, connection='pair', base_url='http://fixture', path='/echo',
                                     deadline=time.monotonic()+2, idle_timeout=.1 if side == 'sender' else 1)
        iterator = response.aiter_bytes()
        assert await anext(iterator) == b': keepalive\n\n'
        with pytest.raises(TransportFailure):
            await asyncio.wait_for(anext(iterator), .8)
        await response.aclose()
        assert closed == [True] and transport.closed == 1
    run_pair(pair, app, exercise, lifetime=2, idle_timeout=.1 if side == 'receiver' else 1)


def test_stream_opening_stall_uses_opening_budget(pair):
    closed = []
    async def app(scope, receive, send):
        try: await asyncio.Event().wait()
        finally: closed.append(True)
    async def exercise(client, transport):
        with pytest.raises(TransportFailure) as error:
            await asyncio.wait_for(open_stream(client, connection='pair', base_url='http://fixture', path='/echo',
                                   deadline=time.monotonic()+2, open_timeout=.1), .8)
        assert error.value.reason == 'timeout' and closed == [True]
    run_pair(pair, app, exercise, lifetime=2)


@pytest.mark.parametrize('side', ['sender', 'receiver'])
def test_heartbeats_never_extend_absolute_expiry(pair, side):
    closed = []
    async def app(scope, receive, send):
        try:
            await send(dict(type='http.response.start', status=200, headers=[(b'content-type', b'text/event-stream')]))
            while True:
                await send(dict(type='http.response.body', body=b': keepalive\n\n', more_body=True))
                await asyncio.sleep(.02)
        finally: closed.append(True)
    async def exercise(client, transport):
        response = await open_stream(client, connection='pair', base_url='http://fixture', path='/echo',
                                     deadline=time.monotonic()+(.2 if side == 'sender' else 2), idle_timeout=1)
        async def collect():
            return [chunk async for chunk in response.aiter_bytes()]
        with pytest.raises(TransportFailure):
            await asyncio.wait_for(collect(), .8)
        await response.aclose()
        assert closed == [True] and transport.closed == 1
    run_pair(pair, app, exercise, lifetime=.2 if side == 'receiver' else 2, idle_timeout=1)


@pytest.mark.parametrize('value', [0, -1, True, float('nan'), float('inf'), 3601])
def test_stream_budgets_fail_closed_before_io(pair, value):
    async def app(*args): raise AssertionError('must not dispatch')
    async def exercise(client, transport):
        for field in ('open_timeout', 'idle_timeout'):
            with pytest.raises(TransportFailure):
                await open_stream(client, connection='pair', base_url='http://fixture', path='/echo',
                                  deadline=time.monotonic()+1, **{field: value})
        assert transport.calls == 0 and client._stream_count == 0
    run_pair(pair, app, exercise)


@pytest.mark.parametrize('compressed',[False,True])
def test_real_receiver_first_event_before_completion_and_cookies(pair,compressed):
    release=None
    async def app(scope,receive,send):
        assert (b'x-forwarded-proto',b'https') in scope['headers']
        headers=[(b'content-type',b'text/event-stream'),(b'set-cookie',b'a=1'),(b'set-cookie',b'b=2')]
        if compressed:headers.append((b'content-encoding',b'gzip'))
        await send({'type':'http.response.start','status':200,'headers':headers})
        event=b'data: hello\n\n'
        if compressed:
            import zlib
            encoder=zlib.compressobj(wbits=31)
            first=encoder.compress(event)+encoder.flush(zlib.Z_SYNC_FLUSH)
        else:first=event
        await send({'type':'http.response.body','body':first,'more_body':True})
        await release.wait()
        last=encoder.flush() if compressed else b''
        await send({'type':'http.response.body','body':last})
    async def exercise(client,transport):
        nonlocal release
        release=asyncio.Event()
        response=await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',
                                  headers={'x-forwarded-proto':'https'},deadline=time.monotonic()+1)
        try:
            assert response.headers.get_list('set-cookie')==['a=1','b=2']
            assert 'content-encoding' not in response.headers
            it=response.aiter_bytes()
            assert await anext(it)==b'data: hello\n\n'
            assert not release.is_set()
            release.set()
            assert [chunk async for chunk in it]==[]
        finally:await response.aclose()
        assert transport.calls==1 and transport.closed==1
    run_pair(pair,app,exercise)


@pytest.mark.parametrize('off,stream',[(False,True),(True,False)])
def test_stream_unavailable_never_sends(pair,off,stream):
    async def app(*args):raise AssertionError('must not dispatch')
    async def exercise(client,transport):
        with pytest.raises(TransportFailure) as err:
            await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+1)
        assert err.value.outcome=='not_sent' and transport.calls==0
    run_pair(pair,app,exercise,off=off,stream=stream)


def test_silent_stream_expires_and_releases_without_iteration(pair):
    async def app(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
        await asyncio.Event().wait()
    async def exercise(client,transport):
        released=[]
        response=await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',
                                  deadline=time.monotonic()+.1,release=lambda:released.append(True))
        await asyncio.wait_for(response.closed.wait(),.5)
        await response.aclose();await response.aclose()
        assert released==[True] and transport.closed==1
    run_pair(pair,app,exercise)


def test_header_profile_is_directional_and_digest_bound(pair):
    a,b=contexts(pair,stream=False)
    payload={'method':'GET','path':'/echo','query_b64':'','body_b64':'','status':None,'response_to':None,
             'headers':[{'name':'set-cookie','value_b64':'YT0x'}]}
    with pytest.raises(ValueError):a.validate_outbound_plain('pair','echo',canonical(payload))
    payload.update(status=200,headers=[{'name':'x-forwarded-for','value_b64':'MTI3LjAuMC4x'}])
    with pytest.raises(ValueError):b.validate_response_payload('echo',canonical(payload))
    plain,_=contexts(pair,stream=False,profile=False)
    assert a.plain_policy('pair','echo')!=plain.plain_policy('pair','echo')


def test_absent_and_false_capabilities_preserve_digest(pair):
    da,db,ha,hb,common,catalog=pair[0].inputs
    cfg=common|dict(local=identity(pair[1]),credential_snapshot_sha256=ha)
    old=NativeContext(str(da),canonical(cfg),canonical(catalog))
    explicit=copy.deepcopy(catalog);explicit[0].update(response_stream=False,proxy_headers=False)
    same=NativeContext(str(da),canonical(cfg),canonical(explicit))
    assert old.revisions()==same.revisions()


@pytest.mark.parametrize('fault',['missing','mismatch','duplicate','gzip_truncated','gzip_expansion','header_direction'])
def test_stream_response_faults_fail_closed(pair,fault):
    a,_=contexts(pair)
    closed=[]
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            if fault=='gzip_truncated':yield gzip.compress(b'data: x\n\n')[:-5]
            elif fault=='gzip_expansion':yield gzip.compress(b'x'*(2*1024*1024))
            else:yield b'data: x\n\n'
        async def aclose(self):closed.append(True)
    def handler(request):
        metadata=request.headers[POLICY_HEADER.decode()]
        h=[(b'content-type',b'text/event-stream'),(POLICY_HEADER,metadata.encode())]
        if fault=='missing':h=h[:1]
        if fault=='mismatch':h[1]=(POLICY_HEADER,b'wrong')
        if fault=='duplicate':h.append(h[1])
        if fault.startswith('gzip'):h.append((b'content-encoding',b'gzip'))
        if fault=='header_direction':h.append((b'x-forwarded-for',b'127.0.0.1'))
        return httpx.Response(200,headers=h,stream=Body())
    async def run():
        jobs=NativeJobs()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client=Client(a,http,jobs=jobs,trusted_egress=fixture_egress(a))
                with pytest.raises(TransportFailure):
                    response=await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+1)
                    async for _ in response.aiter_bytes():pass
            assert closed==[True]
        finally:await jobs.close()
    asyncio.run(run())


def test_revocation_interrupts_silent_read(pair):
    a,_=contexts(pair)
    class Source:
        active=True
        def acquire(self):
            if not self.active:raise ValueError('revoked')
            return a
    source=Source();closed=[]
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b''
        async def aclose(self):closed.append(True)
    def handler(request):return httpx.Response(200,headers={'content-type':'text/event-stream',POLICY_HEADER.decode():request.headers[POLICY_HEADER.decode()]},stream=Body())
    async def run():
        jobs=NativeJobs()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client=Client(source,http,jobs=jobs,trusted_egress=fixture_egress(source))
                response=await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+1)
                task=asyncio.create_task(anext(response.aiter_bytes()))
                await asyncio.sleep(0);source.active=False
                with pytest.raises(TransportFailure):await asyncio.wait_for(task,.5)
                assert closed==[True]
        finally:await jobs.close()
    asyncio.run(run())


def test_receiver_failure_after_start_never_sends_second_status(pair):
    a,b=contexts(pair);messages=[]
    async def app(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
        raise RuntimeError('fixture')
    async def run():
        middleware=Middleware(app,b,None,trusted_ingress=lambda s:TrustedPeer('pair',pair[1]['node_id'],pair[1]['app_instance_id']))
        count=0
        async def receive():
            nonlocal count
            count+=1
            if count==1:return {'type':'http.request','body':b''}
            await asyncio.Event().wait()
        async def send(m):messages.append(m)
        try:
            with pytest.raises(RuntimeError):await middleware({'type':'http','method':'GET','path':'/echo','headers':[(POLICY_HEADER,a.plain_policy('pair','echo').encode())]},receive,send)
            assert len([m for m in messages if m['type']=='http.response.start'])==1
        finally:await middleware.jobs.close()
    asyncio.run(run())


@pytest.mark.parametrize('phase',['headers','bounded_body'])
def test_revoke_interrupts_opening(pair,phase):
    a,_=contexts(pair)
    class Source:
        active=True
        def acquire(self):
            if not self.active:raise ValueError('revoked')
            return a
    source=Source();entered=None
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set();await asyncio.Event().wait();yield b''
    async def handler(request):
        if phase=='headers':entered.set();await asyncio.Event().wait()
        return httpx.Response(503,headers={'content-type':'application/json',POLICY_HEADER.decode():request.headers[POLICY_HEADER.decode()]},stream=Body())
    async def run():
        nonlocal entered
        entered=asyncio.Event();jobs=NativeJobs();released=[]
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client=Client(source,http,jobs=jobs,trusted_egress=fixture_egress(source))
                task=asyncio.create_task(open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+10,release=lambda:released.append(True)))
                await entered.wait();source.active=False
                with pytest.raises(TransportFailure) as error:await asyncio.wait_for(task,.5)
                assert error.value.outcome=='outcome_unknown'
                assert released==[True] and client._stream_count==0
        finally:await jobs.close()
    asyncio.run(run())


def test_cleanup_failure_does_not_reclassify_possible_send(pair):
    a,_=contexts(pair)
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):yield b''
        async def aclose(self):raise RuntimeError('cleanup fixture')
    def handler(request):return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Body())
    async def run():
        jobs=NativeJobs()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client=Client(a,http,jobs=jobs,trusted_egress=fixture_egress(a))
                with pytest.raises(TransportFailure) as error:await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+1)
                assert error.value.outcome=='outcome_unknown' and client._stream_count==0
        finally:await jobs.close()
    asyncio.run(run())


def test_low_level_stream_capacity(pair):
    a,_=contexts(pair);calls=[]
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):await asyncio.Event().wait();yield b''
    def handler(request):
        calls.append(True)
        return httpx.Response(200,headers={'content-type':'text/event-stream',POLICY_HEADER.decode():request.headers[POLICY_HEADER.decode()]},stream=Body())
    async def run():
        jobs=NativeJobs();responses=[]
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client=Client(a,http,jobs=jobs,trusted_egress=fixture_egress(a))
                for _ in range(16):responses.append(await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+5))
                with pytest.raises(TransportFailure) as error:await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+5)
                assert error.value.outcome=='not_sent' and len(calls)==16
                for r in responses:await r.aclose()
                assert client._stream_count==0
        finally:
            for r in responses:await r.aclose()
            await jobs.close()
    asyncio.run(run())


@pytest.mark.parametrize('header',[b'connection',b'transfer-encoding',b'x-app-security-policy'])
def test_receiver_rejects_framing_or_reserved_headers_before_start(pair,header):
    async def app(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream'),(header,b'forged')]})
        await send({'type':'http.response.body','body':b'bad'})
    async def exercise(client,transport):
        with pytest.raises(TransportFailure):await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+1)
        assert transport.calls==1
    run_pair(pair,app,exercise)


def test_eof_does_not_release_or_stop_final_send_deadline(pair):
    a,_=contexts(pair);released=[]
    def handler(request):return httpx.Response(200,headers={'content-type':'text/event-stream',POLICY_HEADER.decode():request.headers[POLICY_HEADER.decode()]},stream=httpx.ByteStream(b'data: x\n\n'))
    async def run():
        jobs=NativeJobs()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client=Client(a,http,jobs=jobs,trusted_egress=fixture_egress(a))
                response=await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+.15,release=lambda:released.append(True))
                async def browser():
                    assert [x async for x in response.aiter_bytes()]==[b'data: x\n\n']
                    assert not released
                    await asyncio.Event().wait()
                with pytest.raises(TransportFailure):await asyncio.wait_for(response.run(browser()),.5)
                assert released==[True]
        finally:await jobs.close()
    asyncio.run(run())


def test_already_aborted_never_schedules_operation():
    from app_security_shell.streaming import guarded
    calls=[]
    async def run():
        closed=asyncio.Event();closed.set()
        async def operation():calls.append(True)
        with pytest.raises(TransportFailure):await guarded(operation(),closed)
        assert not calls
    asyncio.run(run())


def test_cancellation_at_open_handoff_closes_created_response(pair,monkeypatch):
    import app_security_shell.streaming as module
    a,_=contexts(pair);closed=[]
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):yield b'x'
        async def aclose(self):closed.append(True)
    def handler(request):return httpx.Response(200,headers={'content-type':'text/event-stream',POLICY_HEADER.decode():request.headers[POLICY_HEADER.decode()]},stream=Body())
    async def interrupted(awaitable,event):
        await awaitable
        raise asyncio.CancelledError
    monkeypatch.setattr(module,'guarded',interrupted)
    async def run():
        jobs=NativeJobs()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client=Client(a,http,jobs=jobs,trusted_egress=fixture_egress(a))
                with pytest.raises(asyncio.CancelledError):await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+1)
                assert closed==[True] and client._stream_count==0
        finally:await jobs.close()
    asyncio.run(run())


def test_binding_owns_stream_until_delivery_and_drain(pair):
    from app_security_shell.binding import AppSecurityBinding
    from app_security_shell.receiver import GuardedASGI
    da,db,ha,hb,common,catalog=pair[0].inputs
    common=copy.deepcopy(common);catalog=copy.deepcopy(catalog)
    catalog[0].update(method='GET',response_stream=True)
    common['endpoint_overrides']=[dict(connection_id='pair',endpoint_id='echo',extra_encryption=False)]
    ap={'app_security':common|dict(local=identity(pair[1]),credential_snapshot_sha256=ha),
        'app_security_binding':dict(schema_version=1,destinations=[dict(connection_id='pair',base_url='http://fixture')],outbound_endpoints=catalog)}
    bp={'app_security':common|dict(local=identity(pair[2]),credential_snapshot_sha256=hb),
        'app_security_binding':dict(schema_version=1,destinations=[],outbound_endpoints=[])}
    async def run():
        a=AppSecurityBinding(snapshot_provider=lambda:ap,directory_provider=lambda:da,receiver_catalog=[])
        b=AppSecurityBinding(snapshot_provider=lambda:bp,directory_provider=lambda:db,receiver_catalog=catalog)
        a.install_integration(trusted_egress=fixture_egress(a))
        b.install_integration(trusted_ingress=lambda s:TrustedPeer('pair',pair[1]['node_id'],pair[1]['app_instance_id']))
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
            await send({'type':'http.response.body','body':b'data: x\n\n'})
        try:
            async with httpx.AsyncClient(transport=IncrementalTransport(GuardedASGI(app,binding=b))) as http:
                response=await a.open_stream('pair','/echo',deadline=time.monotonic()+2,http_client=http,recipient_app='signatory')
                sent=asyncio.Event()
                async def browser():
                    assert [x async for x in response.aiter_bytes()]==[b'data: x\n\n']
                    sent.set();await asyncio.Event().wait()
                task=asyncio.create_task(response.run(browser()))
                await sent.wait()
                assert a._count==1 and a._streams==1
                await asyncio.wait_for(a.close(),.5)
                with pytest.raises(TransportFailure):await task
                assert a._count==0 and a._streams==0 and not http.is_closed
        finally:await a.close();await b.close()
    asyncio.run(run())


def test_stream_open_timeout_preserves_reason(pair):
    a,_=contexts(pair)
    async def handler(request):await asyncio.sleep(1)
    async def run():
        jobs=NativeJobs()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client=Client(a,http,jobs=jobs,trusted_egress=fixture_egress(a))
                with pytest.raises(TransportFailure) as error:await open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+.05)
                assert error.value.reason=='timeout' and error.value.outcome=='outcome_unknown'
        finally:await jobs.close()
    asyncio.run(run())


def test_cancellation_during_final_watcher_teardown_closes_response(pair,monkeypatch):
    import app_security_shell.streaming as module
    a,_=contexts(pair);closed=[];teardown=None
    original=asyncio.gather
    def gather(*tasks,**kwargs):
        if len(tasks)==1 and getattr(tasks[0].get_coro(),'__name__','')=='watch_open':
            async def stalled():
                teardown.set();await asyncio.Event().wait()
            return stalled()
        return original(*tasks,**kwargs)
    monkeypatch.setattr(module.asyncio,'gather',gather)
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):yield b'x'
        async def aclose(self):closed.append(True)
    def handler(request):return httpx.Response(200,headers={'content-type':'text/event-stream',POLICY_HEADER.decode():request.headers[POLICY_HEADER.decode()]},stream=Body())
    async def run():
        nonlocal teardown
        teardown=asyncio.Event();jobs=NativeJobs()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client=Client(a,http,jobs=jobs,trusted_egress=fixture_egress(a))
                task=asyncio.create_task(open_stream(client,connection='pair',base_url='http://fixture',path='/echo',deadline=time.monotonic()+1))
                await teardown.wait();task.cancel()
                with pytest.raises(asyncio.CancelledError):await task
                assert closed==[True] and client._stream_count==0
        finally:await jobs.close()
    asyncio.run(run())


@pytest.mark.parametrize('cancellation_kind', ['anyio', 'asyncio'])
def test_guarded_drains_owned_cleanup_through_repeated_cancellation(cancellation_kind):
    import anyio
    from app_security_shell.streaming import guarded

    async def run():
        started, cleaning, release, finished = [asyncio.Event() for _ in range(4)]
        calls = []
        async def operation():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                calls.append('cleanup')
                cleaning.set()
                await release.wait()
                finished.set()
        async def owner():
            await guarded(operation(), asyncio.Event())
        if cancellation_kind == 'asyncio':
            task = asyncio.create_task(owner())
            await started.wait()
            task.cancel()
            await cleaning.wait()
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and not finished.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        else:
            async def unlock():
                await cleaning.wait()
                await asyncio.sleep(.01)
                release.set()
            unlocker = asyncio.create_task(unlock())
            async with anyio.create_task_group() as group:
                group.start_soon(owner)
                await started.wait()
                group.cancel_scope.cancel()
            await unlocker
        assert finished.is_set() and calls == ['cleanup']
    asyncio.run(run())
