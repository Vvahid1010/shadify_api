"""Neutral application fixtures exercise the actual installed generic binding."""
import asyncio
import copy
import json
import sys
import time

import httpx
import pytest
from app_security_shell.binding import AppSecurityBinding
from app_security_shell.receiver import GuardedASGI
from app_security_shell.transport import TrustedPeer, TransportFailure
from test_pair import pair, identity, FixtureReplay, fixture_egress


@pytest.fixture
def bindings(pair,request):
    mode=getattr(request,'param','on')
    adir,bdir,ah,bh,common,_=pair[0].inputs
    common=copy.deepcopy(common)
    # Neutral app names (synthetic private-file app names must match for ON,
    # so real-key tests use the fixture's two names, neutral roles also test OFF).
    catalog=[dict(endpoint_id=method.lower(),receiver_app='signatory',permitted_peer_apps=['wallet_manager'],method=method,path='/items/{item_id}',headers=['content-type','x-cell-id','x-signatory-trace-id','x-repeat','location'],off_supported=True) for method in ('GET','HEAD','OPTIONS','POST','PUT','PATCH','DELETE')]
    common['connections'][0]['endpoint_ids']=[e['endpoint_id'] for e in catalog]
    if mode=='off':
        common['endpoint_overrides']=[dict(connection_id='pair',endpoint_id=e['endpoint_id'],extra_encryption=False) for e in catalog]
        for side in ('sender','recipient'):common['connections'][0][side].pop('keys')
        adir=adir/'absent';bdir=bdir/'absent';ah=bh=''
    acfg=copy.deepcopy(common)|dict(local=identity(pair[1]),credential_snapshot_sha256=ah)
    bcfg=copy.deepcopy(common)|dict(local=identity(pair[2]),credential_snapshot_sha256=bh)
    ap=dict(app_security=acfg,app_security_binding=dict(schema_version=1,destinations=[dict(connection_id='pair',base_url='http://fixture/prefix')],outbound_endpoints=catalog))
    bp=dict(app_security=bcfg,app_security_binding=dict(schema_version=1,destinations=[],outbound_endpoints=[]))
    a=AppSecurityBinding(snapshot_provider=lambda:ap,directory_provider=lambda:adir,receiver_catalog=[])
    b=AppSecurityBinding(snapshot_provider=lambda:bp,directory_provider=lambda:bdir,receiver_catalog=catalog)
    a.install_integration(trusted_egress=fixture_egress(a))
    b.install_integration(trusted_ingress=lambda s:TrustedPeer('pair',pair[1]['node_id'],pair[1]['app_instance_id']),clock_bounds=lambda:True,storage_bounds=lambda:True)
    b._replay=FixtureReplay()
    yield a,b,ap,bp
    asyncio.run(a.close());asyncio.run(b.close())


@pytest.mark.parametrize('bindings',['on','off'],indirect=True)
def test_current_route_exposes_native_transport_binding_only_when_requested(bindings):
    sender, _, _, _ = bindings
    asyncio.run(sender.ready())
    ordinary = sender.route_metadata('pair', 'GET', '/items/one', recipient_app='signatory')
    assert 'transport_binding' not in ordinary
    selected = sender.route_metadata(
        'pair', 'GET', '/items/one', recipient_app='signatory',
        include_transport_binding=True,
    )
    assert {key: value for key, value in selected.items() if key != 'transport_binding'} == ordinary
    assert selected['transport_binding'] == sender._select('pair', None, 'signatory')[0].acquire().transport_binding('pair')
    with pytest.raises(ValueError):
        sender.route_metadata('pair', 'GET', '/items/one', recipient_app='wrong', include_transport_binding=True)


@pytest.mark.parametrize('bindings',['on','off'],indirect=True)
def test_methods_queries_bytes_and_contaminated_clients(bindings):
    a,b,ap,bp=bindings
    calls=[]
    async def business(scope,receive,send):
        body=(await receive())['body'];calls.append((scope,body))
        assert scope['path']=='/prefix/items/abc'
        assert scope['query_string']==b'x=1&x=&x=2+a%20b%2f'
        assert [(k,v) for k,v in scope['headers'] if k==b'x-repeat']==[(b'x-repeat',b'a'),(b'x-repeat',b'b')]
        assert not any(k in (b'cookie',b'authorization',b'x-poison') for k,v in scope['headers'])
        await send(dict(type='http.response.start',status=200,headers=[(b'content-type',b'application/octet-stream')]))
        await send(dict(type='http.response.body',body=b'' if scope['method']=='HEAD' else body))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(GuardedASGI(business,binding=b),root_path='/prefix'),headers={'x-poison':'bad'},cookies={'session':'bad'},params={'bad':'value'},auth=('bad','bad')) as http:
            for method in ('GET','HEAD','OPTIONS','POST','PUT','PATCH','DELETE'):
                r=await a.request('pair',method,'/items/abc',query=b'x=1&x=&x=2+a%20b%2f',body=b'\x00\xfffixture',headers=[(b'content-type',b'application/octet-stream'),(b'x-repeat',b'a'),(b'x-repeat',b'b')],http_client=http)
                assert r.status_code==200 and r.content==(b'' if method=='HEAD' else b'\x00\xfffixture')
        assert len(calls)==7
    asyncio.run(run())


@pytest.mark.parametrize('fault',['destination','missing','unknown','incoming','local_catalog','type','ambiguous','malformed','identity'])
def test_invalid_projection_retains_active_snapshot(bindings,fault):
    a,b,ap,bp=bindings
    async def run():
        await a.ready();previous=a.acquire();update=copy.deepcopy(ap)
        if fault=='destination':update['app_security_binding']['destinations'][0]['base_url']='http://changed'
        elif fault=='missing':update['app_security_binding']['destinations']=[]
        elif fault=='unknown':update['app_security_binding']['surprise']=1
        elif fault=='incoming':update['app_security_binding']['destinations'][0]['connection_id']='incoming'
        elif fault=='local_catalog':update['app_security_binding']['outbound_endpoints'][0]['receiver_app']='wallet_manager'
        elif fault=='type':update['app_security_binding']['schema_version']=True
        elif fault=='ambiguous':
            e=copy.deepcopy(update['app_security_binding']['outbound_endpoints'][0]);e.update(endpoint_id='other',path='/items/{other}')
            update['app_security_binding']['outbound_endpoints'].append(e)
        elif fault=='malformed':update['app_security_binding']['outbound_endpoints'][0]['path']='/items/x{bad}'
        elif fault=='identity':update['app_security']['local']['node_id']='77777777-7777-4777-8777-777777777777'
        with pytest.raises(Exception):await a.jobs.run(a.prepare,update)
        assert a.acquire() is previous
        candidate=await a.jobs.run(a.prepare,copy.deepcopy(ap));a.install(candidate)
        assert a.acquire() is previous
    asyncio.run(run())


def test_static_guard_without_config_native_and_websocket():
    calls=[]
    e=dict(endpoint_id='item',receiver_app='neutral',permitted_peer_apps=['sender'],method='GET',path='/items/{id}',headers=[],off_supported=True)
    b=AppSecurityBinding(snapshot_provider=lambda:{},directory_provider=lambda:'',receiver_catalog=[e])
    async def business(scope,receive,send):
        calls.append(scope['path'])
        await send(dict(type='http.response.start',status=200,headers=[]));await send(dict(type='http.response.body',body=b'ok'))
    async def run():
        guard=GuardedASGI(business,binding=b)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(guard)) as http:
            for method,path in [('GET','/items/1'),('POST','/items/1'),('HEAD','/items/1'),('GET','/items/1/'),('GET','/items/%61'),('POST','/_app-security/v1/message')]:
                assert (await http.request(method,'http://fixture'+path)).status_code==503
            assert (await http.get('http://fixture/health')).status_code==200
        messages=[]
        async def send(m):messages.append(m)
        await guard(dict(type='websocket',path='/items/1'),None,send)
        assert messages==[dict(type='websocket.close',code=1008)]
        assert calls==['/health']
        await b.close()
    asyncio.run(run())


def test_neutral_multi_app_and_same_app_instances():
    def who(n,app):
        return dict(node_id=f'{n:08d}-1111-4111-8111-111111111111',app_id=app,app_instance_id=f'{n:08d}-2222-4222-8222-222222222222',generation=1)
    people={'a':who(1,'alpha'),'b':who(2,'beta'),'b2':who(3,'beta'),'g':who(4,'gamma')}
    endpoints={name:dict(endpoint_id=name+'.get',receiver_app=name,permitted_peer_apps=['alpha','beta','gamma'],method='GET',path='/items/{id}',headers=['content-type'],off_supported=True) for name in ('alpha','beta','gamma')}
    edges=[('ab','a','b'),('ab2','a','b2'),('ag','a','g'),('ga','g','a'),('ba','b','a')]
    connections=[dict(connection_id=k,policy_revision=1,sender={'identity':people[x]},recipient={'identity':people[y]},endpoint_ids=[people[y]['app_id']+'.get']) for k,x,y in edges]
    bindings={};projections={}
    for name,local in people.items():
        cs=[c for c in connections if local in (c['sender']['identity'],c['recipient']['identity'])]
        remote={c['recipient']['identity']['app_id'] for c in cs if c['sender']['identity']==local}
        cfg=dict(contract_version=1,extra_encryption_default=True,credentials_file='app_security.credentials.json',credential_snapshot_sha256='',local=local,connections=cs,endpoint_overrides=[dict(connection_id=c['connection_id'],endpoint_id=c['endpoint_ids'][0],extra_encryption=False) for c in cs])
        projection=dict(app_security=cfg,app_security_binding=dict(schema_version=1,destinations=[dict(connection_id=k,base_url='http://'+y) for k,x,y in edges if x==name],outbound_endpoints=[endpoints[r] for r in sorted(remote)]))
        projections[name]=projection
        b=AppSecurityBinding(snapshot_provider=lambda p=projection:p,directory_provider=lambda:'/absent',receiver_catalog=[endpoints[local['app_id']]])
        b.install_integration(trusted_egress=fixture_egress(b),trusted_ingress=lambda scope:scope['fixture_peer'])
        bindings[name]=b
    async def run():
        apps={}
        for name,b in bindings.items():
            async def business(scope,receive,send,name=name):
                await send(dict(type='http.response.start',status=200,headers=[(b'content-type',b'text/plain')]))
                await send(dict(type='http.response.body',body=name.encode()))
            apps[name]=GuardedASGI(business,binding=b)
        async def request(edge):
            key,sender,recipient=edge
            async def channel(scope,receive,send):
                scope=dict(scope,fixture_peer=TrustedPeer(key,people[sender]['node_id'],people[sender]['app_instance_id']))
                await apps[recipient](scope,receive,send)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(channel)) as http:
                result=await bindings[sender].request(key,'GET','/items/one',http_client=http)
                assert result.text==recipient
                with pytest.raises(TransportFailure):await bindings[sender].request(key,'GET','/items/two',expected_base='http://wrong',http_client=http)
        try:
            await asyncio.gather(*(request(edge) for edge in edges))
            assert all(b._replay is None for b in bindings.values())
        finally:
            for b in bindings.values():await b.close()
    asyncio.run(run())


@pytest.mark.parametrize('bindings',['on','off'],indirect=True)
@pytest.mark.parametrize('status',[204,304,307])
def test_bodyless_and_redirect_responses(bindings,status):
    a,b,_,_=bindings;calls=[]
    async def business(scope,receive,send):
        calls.append(1)
        await send(dict(type='http.response.start',status=status,headers=[(b'location',b'/elsewhere')]))
        await send(dict(type='http.response.body',body=b''))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(GuardedASGI(business,binding=b),root_path='/prefix'),follow_redirects=True) as http:
            response=await a.request('pair','GET','/items/one',http_client=http)
            assert response.status_code==status and response.content==b'' and response.headers['location']=='/elsewhere'
            assert calls==[1]
    asyncio.run(run())


@pytest.mark.parametrize('bindings',['on','off'],indirect=True)
def test_snapshot_replacement_before_send_is_not_sent(bindings):
    a,b,ap,_=bindings
    async def run():
        await a.ready()
        old=a._active
        selected=__import__('app_security_shell.binding',fromlist=['_Selected'])._Selected(a,old)
        update=copy.deepcopy(ap)
        update['app_security']['connections'][0]['policy_revision']=2
        update['app_security_binding']['destinations'][0]['base_url']='http://new'
        a.install(await a.jobs.run(a.prepare,update))
        with pytest.raises(ValueError):selected.acquire()
        assert a._active.destinations['pair']=='http://new'
        a.revoke(ap['app_security']['local']['app_instance_id'],1)
        with pytest.raises(ValueError):await a.ready()
    asyncio.run(run())


def test_encoded_template_and_prefixed_root_never_bypass():
    async def run():
        for template in ('/items/{id}','/'):
            calls=[]
            e=dict(endpoint_id='item',receiver_app='neutral',permitted_peer_apps=['sender'],method='GET',path=template,headers=[],off_supported=True)
            binding=AppSecurityBinding(snapshot_provider=lambda:{},directory_provider=lambda:'',receiver_catalog=[e])
            async def business(scope,receive,send):calls.append(True)
            wrapper=GuardedASGI(business,binding=binding)
            sent=[]
            async def send(m):sent.append(m)
            async def receive():return dict(type='http.request',body=b'')
            path='/items/a%2Fb' if template!='/' else '/prefix'
            raw=b'/items/a%252Fb' if template!='/' else b'/prefix'
            await wrapper(dict(type='http',method='GET',path=path,raw_path=raw,root_path='' if template!='/' else '/prefix'),receive,send)
            assert sent[0]['status']==503 and not calls
            await binding.close()
    asyncio.run(run())


def test_slow_initialization_keeps_loop_and_deadline_available(bindings):
    from threading import Event
    a,b,ap,bp=bindings
    entered,release=Event(),Event()
    def provider():entered.set();release.wait(5);return ap
    a._provider=provider
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError('must not send')))) as http:
            task=asyncio.create_task(a.request('pair','GET','/items/one',http_client=http,timeout=.1))
            try:
                assert await asyncio.to_thread(entered.wait,2)
                await asyncio.sleep(.01) # Loop must remain available before release.
                with pytest.raises(TransportFailure) as error:await asyncio.wait_for(task,.5)
                assert error.value.outcome=='not_sent'
            finally:release.set()
    asyncio.run(run())


def test_admission_rejection_send_has_short_bound(bindings):
    a,b,_,_=bindings
    async def run():
        async def forbidden(*a):raise AssertionError('business called')
        async def receive():return dict(type='http.request',body=b'bad')
        async def send(message):await asyncio.Event().wait()
        wrapper=GuardedASGI(forbidden,binding=b,rejection_timeout=.02)
        await asyncio.wait_for(wrapper(dict(type='http',method='POST',path='/_app-security/v1/message',headers=[(b'content-type',b'application/app-security+json')]),receive,send),1)
        assert b._count==0
    asyncio.run(run())


@pytest.mark.parametrize('bindings',['on','off'],indirect=True)
def test_sync_all_methods_and_drain_admitted_response(bindings):
    from threading import Event
    a,b,_,_=bindings
    calls=[]
    async def business(scope,receive,send):
        calls.append(scope['method'])
        assert scope['query_string']==b'v=&v=a+b%20c'
        assert not any(k in (b'cookie',b'authorization',b'x-poison') for k,v in scope['headers'])
        await send(dict(type='http.response.start',status=200,headers=[]))
        await send(dict(type='http.response.body',body=b'' if scope['method']=='HEAD' else (await receive())['body']))
    guard=GuardedASGI(business,binding=b)
    def channel(req):
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(guard,root_path='/prefix')) as http:
                r=await http.send(req)
                return httpx.Response(r.status_code,headers=r.headers,stream=httpx.ByteStream(r.content))
        return asyncio.run(call())
    with httpx.Client(transport=httpx.MockTransport(channel),headers={'x-poison':'bad'},cookies={'bad':'bad'},params={'bad':'bad'},auth=('bad','bad')) as http:
        for method in ('GET','HEAD','OPTIONS','POST','PUT','PATCH','DELETE'):
            r=a.request_sync('pair',method,'/items/sync',query=b'v=&v=a+b%20c',body=b'opaque',http_client=http)
            assert r.content==(b'' if method=='HEAD' else b'opaque')
    assert len(calls)==7


@pytest.mark.parametrize('bindings',['on','off'],indirect=True)
def test_close_waits_for_admitted_response_and_never_reopens(bindings):
    a,b,_,_=bindings
    async def run():
        admitted,release=asyncio.Event(),asyncio.Event()
        async def business(scope,receive,send):
            admitted.set();await release.wait()
            await send(dict(type='http.response.start',status=200,headers=[]))
            await send(dict(type='http.response.body',body=b'drained'))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(GuardedASGI(business,binding=b),root_path='/prefix')) as http:
            request=asyncio.create_task(a.request('pair','POST','/items/one',http_client=http))
            await admitted.wait()
            closing=asyncio.create_task(b.close());await asyncio.sleep(.01)
            assert not closing.done()
            with pytest.raises(ValueError):await b.ready()
            release.set()
            assert (await request).content==b'drained'
            await closing
            assert not http.is_closed
            with pytest.raises(ValueError):await b.ready()
    asyncio.run(run())


def test_missing_native_keeps_static_guard_and_health(tmp_path):
    import subprocess
    program='''import sys,asyncio
sys.modules['app_security_shell._native']=None
from app_security_shell.binding import AppSecurityBinding
from app_security_shell.receiver import GuardedASGI
b=AppSecurityBinding(snapshot_provider=lambda:{},directory_provider=lambda:'',receiver_catalog=[])
calls=[]
async def app(s,r,w):calls.append(s['path'])
async def run():
 g=GuardedASGI(app,binding=b);out=[]
 async def send(m):out.append(m)
 await g(dict(type='http',method='GET',path='/health'),None,send)
 await g(dict(type='http',method='POST',path='/_app-security/v1/message'),None,send)
 assert calls==['/health'] and out[0]['status']==503
asyncio.run(run())
'''
    result=subprocess.run([sys.executable,'-B','-c',program],capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr.decode()


@pytest.mark.parametrize('bindings',['on','off'],indirect=True)
def test_concurrent_sync_async_and_preflight_canaries(bindings,capsys):
    a,b,_,_=bindings
    async def business(scope,receive,send):
        await send(dict(type='http.response.start',status=200,headers=[]))
        await send(dict(type='http.response.body',body=b'pair'))
    guard=GuardedASGI(business,binding=b)
    def channel(req):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(guard,root_path='/prefix')) as http:
                r=await http.send(req)
                return httpx.Response(r.status_code,headers=r.headers,stream=httpx.ByteStream(r.content))
        return asyncio.run(run())
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(guard,root_path='/prefix')) as http:
            def sync():
                with httpx.Client(transport=httpx.MockTransport(channel)) as client:
                    return a.request_sync('pair','GET','/items/sync',http_client=client)
            responses=await asyncio.gather(a.request('pair','GET','/items/async',http_client=http),asyncio.to_thread(sync))
            assert [r.content for r in responses]==[b'pair',b'pair']
            for kwargs in (dict(path='http://canary-secret'),dict(path='/items/one',headers={'x-app-security-policy':'canary-secret'}),dict(path='/items/one',query=b'canary-secret\n')):
                with pytest.raises(TransportFailure) as error:await a.request('pair','GET',http_client=http,**kwargs)
                assert str(error.value)=='app_security_not_sent' and error.value.outcome=='not_sent'
            assert 'canary-secret' not in repr(a.status())
    asyncio.run(run())
    captured=capsys.readouterr()
    assert 'canary-secret' not in captured.out+captured.err
