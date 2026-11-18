"""Endpoint-specific finite response limits, real native contexts, synthetic peers."""
import asyncio
import copy
import json
import time
import httpx
import pytest
from test_pair import pair, canonical, fixture_egress, fixture_ingress, FixtureReplay, payload
from app_security_shell._native import NativeContext
from app_security_shell.routing import normalized_catalog, validate_catalog
from app_security_shell.transport import Client, Middleware, TransportFailure

MIB = 1048576

@pytest.mark.parametrize('off', [False, True])
@pytest.mark.parametrize('limit,size', [(MIB,MIB),(MIB,MIB+1),(8*MIB,MIB+1),(8*MIB,8*MIB),(8*MIB,8*MIB+1)])
def test_response_size_boundary(pair, off, limit, size):
    build,alice,bob=pair
    catalog=build.inputs[-1]
    catalog[0].update(method='GET', max_response_bytes=limit)
    a,b=build(off=off)
    body=b'"'+b'x'*(size-2)+b'"'
    reached=[]
    async def app(scope,receive,send):
        reached.append(True)
        await send(dict(type='http.response.start',status=200,headers=[(b'content-type',b'application/json')]))
        await send(dict(type='http.response.body',body=body))
    async def run():
        guard=Middleware(app,b,FixtureReplay(),trusted_ingress=fixture_ingress(alice))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(guard)) as http:
            client=Client(a,http,trusted_egress=fixture_egress(a))
            response=await client.request(connection='pair',base_url='http://fixture',method='GET',path='/echo')
            assert reached == [True]
            if size<=limit:
                assert response.status_code==200
                assert response.content==body
            else:
                assert response.status_code==502
                assert response.json()=={'error':{'code':'response_too_large','response_bytes':size,'limit_bytes':limit}}
    asyncio.run(run())

@pytest.mark.parametrize('off',[False,True])
def test_large_response_allowance_never_enlarges_request(pair,off):
    build,alice,bob=pair
    build.inputs[-1][0].update(method='GET',max_response_bytes=8*MIB)
    a,b=build(off=off)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:pytest.fail('oversized request sent'))) as http:
            client=Client(a,http,trusted_egress=fixture_egress(a))
            with pytest.raises(TransportFailure):
                await client.request(connection='pair',base_url='http://fixture',method='GET',path='/echo',body=b'x'*(MIB+1))
    asyncio.run(run())

@pytest.mark.parametrize('invalid',[True,0,MIB-1,8*MIB+1,'8388608',None])
def test_invalid_limit_denied_by_both_parsers(pair,invalid):
    build,*_=pair
    build.inputs[-1][0].update(method='GET',max_response_bytes=invalid)
    with pytest.raises(ValueError):validate_catalog(build.inputs[-1])
    with pytest.raises(Exception):build()

@pytest.mark.parametrize('changes',[{'method':'POST'},{'method':'GET','response_stream':True}])
def test_override_only_on_finite_reads(pair,changes):
    build,*_=pair
    build.inputs[-1][0].update(max_response_bytes=8*MIB,**changes)
    with pytest.raises(ValueError):validate_catalog(build.inputs[-1])
    with pytest.raises(Exception):build(off=True)

def test_default_digest_compatible_and_custom_limit_bound(pair):
    build,*_=pair
    a,_=build(off=True)
    default=a.transport_binding('pair')[-1]
    build.inputs[-1][0]['max_response_bytes']=MIB
    a,_=build(off=True)
    assert a.transport_binding('pair')[-1]==default
    assert 'max_response_bytes' not in normalized_catalog(build.inputs[-1])[0]
    build.inputs[-1][0].update(method='GET',max_response_bytes=8*MIB)
    a,_=build(off=True)
    assert a.transport_binding('pair')[-1]!=default

@pytest.mark.parametrize('off',[False,True])
def test_synchronous_response_limit(pair,off):
    from app_security_shell.transport import SyncClient, POLICY_HEADER
    from app_security_shell.jobs import NativeJobs
    build,alice,bob=pair
    build.inputs[-1][0].update(method='GET',max_response_bytes=8*MIB)
    a,b=build(off=off)
    data=payload(b'x'*(MIB+1),status=200);data['method']='GET'
    def handler(request):
        if off:
            return httpx.Response(200,stream=httpx.ByteStream(b'x'*(MIB+1)),headers={'content-type':'application/json',POLICY_HEADER.decode():b.plain_policy('pair','echo')})
        ticket=b.receive(request.content,int(time.time()))
        ticket.admit_transport('pair',alice['node_id'],alice['app_instance_id'])
        return httpx.Response(200,stream=httpx.ByteStream(ticket.respond(canonical(data),int(time.time()))),headers={'content-type':'application/app-security+json'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client=SyncClient(a,http,jobs=NativeJobs(),trusted_egress=fixture_egress(a))
        response=client.request(connection='pair',base_url='http://fixture',method='GET',path='/echo')
        assert len(response.content)==MIB+1
        assert response.status_code==200


def test_pending_request_cannot_borrow_another_endpoint_limit(pair):
    build,*_=pair
    catalog=build.inputs[-1]
    catalog[0]['method']='GET'
    catalog.append(dict(catalog[0],endpoint_id='large',path='/large',max_response_bytes=8*MIB))
    build.inputs[-2]['connections'][0]['endpoint_ids'].append('large')
    a,b=build()
    small=payload(b'');small['method']='GET'
    pending=a.begin('pair','echo',canonical(small),100)
    large=dict(small,path='/large')
    other=a.begin('pair','large',canonical(large),100)
    received=b.receive(other.wire(),100)
    response=dict(large,status=200,body_b64=__import__('base64').b64encode(b'x'*(2*MIB)).decode())
    wire=received.respond(canonical(response),100)
    assert len(wire)>2*MIB
    with pytest.raises(ValueError):pending.complete(wire,100)
