from test_pair import fixture_ingress, fixture_egress
import asyncio
import copy
from threading import Event

import httpx
import pytest

from app_security_shell.jobs import NativeJobs
from app_security_shell.transport import Client, Middleware, TransportFailure
from test_pair import pair, canonical, identity, payload, FixtureReplay


def test_mounted_carrier_cannot_bypass_plaintext_gate(pair):
    async def run():
        a,b=pair[0]()
        seen=[]
        async def business(scope,receive,send):
            seen.append(scope['path'])
            await send({'type':'http.response.start','status':204,'headers':[]})
            await send({'type':'http.response.body','body':b''})
        middleware=Middleware(business,b,FixtureReplay(),trusted_ingress=fixture_ingress(pair[1]))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware,root_path='/prefix')) as http:
            assert (await http.post('http://fixture/prefix/echo',json={})).status_code==503
            client=Client(a,http,trusted_egress=fixture_egress(a))
            assert (await client.post_json(connection='pair',base_url='http://fixture/prefix',path='/echo',json_body={})).status_code==204
            assert seen==['/prefix/echo']
            await client.jobs.close()
        await middleware.jobs.close()
    asyncio.run(run())


def test_existing_adapters_observe_revocation_and_keep_admitted_ticket(pair):
    async def run():
        a,b=pair[0](registries=True)
        ticket=a.acquire().begin('pair','echo',canonical(payload()),100)
        admitted=b.acquire().receive(ticket.wire(),100)
        seen=[]
        async def business(scope,receive,send): seen.append(1)
        middleware=Middleware(business,b,FixtureReplay(),trusted_ingress=fixture_ingress(pair[1]))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware)) as http:
            client=Client(a,http,trusted_egress=fixture_egress(a))
            a.revoke(pair[1]['app_instance_id'],1)
            b.revoke(pair[2]['app_instance_id'],1)
            with pytest.raises(TransportFailure) as err:
                await client.post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            assert err.value.outcome=='not_sent'
            assert (await http.post('http://fixture/echo',json={})).status_code==503
            assert not seen
            # Already admitted immutable response contexts can finish during drain.
            response=admitted.respond(canonical(payload(b'',204)),101)
            assert '204' in ticket.complete(response,101)
            await client.jobs.close()
        await middleware.jobs.close()
    asyncio.run(run())


def test_candidate_rejection_and_removed_connection_revision_floor(pair):
    a,_=pair[0](registries=True)
    directory,_,digest,_,common,catalog=pair[0].inputs
    cfg=common | {'local':identity(pair[1]),'credential_snapshot_sha256':digest}
    current=a.acquire()
    invalid=copy.deepcopy(cfg);invalid['connections'][0]['policy_revision']=0
    with pytest.raises(ValueError): a.prepare(str(directory),canonical(invalid),canonical(catalog))
    assert a.acquire() is current
    # Reusing a revision with changed policy is not an update.
    changed=copy.deepcopy(cfg);changed['endpoint_overrides']=[{'connection_id':'pair','endpoint_id':'echo','extra_encryption':False}]
    with pytest.raises(ValueError): a.prepare(str(directory),canonical(changed),canonical(catalog))
    alternate=copy.deepcopy(cfg['connections'][0]);alternate['connection_id']='alternate'
    removed=cfg | {'connections':[alternate]}
    a.install(a.prepare(str(directory),canonical(removed),canonical(catalog)))
    with pytest.raises(ValueError): a.prepare(str(directory),canonical(cfg),canonical(catalog))
    updated=copy.deepcopy(cfg);updated['connections'][0]['policy_revision']=2
    a.install(a.prepare(str(directory),canonical(updated),canonical(catalog)))
    assert a.acquire().route('pair','POST','/echo')==('echo',True)
    a.drain()
    with pytest.raises(ValueError): a.acquire()


def test_cancelled_queue_keeps_capacity_until_dequeued():
    async def run():
        jobs=NativeJobs(active=1,queued=1)
        entered,release=Event(),Event();called=[]
        def blocking():
            entered.set()
            assert release.wait(5)
        first=asyncio.create_task(jobs.run(blocking))
        try:
            while not entered.is_set(): await asyncio.sleep(.001)
            queued=asyncio.create_task(jobs.run(lambda:called.append(1)))
            await asyncio.sleep(.01)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError): await queued
            for _ in range(30):
                with pytest.raises(RuntimeError,match='queue_unavailable'):
                    await jobs.run(lambda:called.append(2))
        finally:
            release.set()
            await first
            await jobs.close()
        assert not called
    asyncio.run(run())


@pytest.mark.parametrize('pause_at',['body','replay'])
def test_revocation_during_admission_never_starts_business(pair,pause_at):
    async def run():
        a,b=pair[0](registries=True)
        paused,resume=asyncio.Event(),asyncio.Event();seen=[];sent=[]
        class Replay(FixtureReplay):
            async def reserve(self,*args):
                if pause_at=='replay':
                    paused.set();await resume.wait()
                await super().reserve(*args)
        async def business(scope,receive,send): seen.append(1)
        wrapper=Middleware(business,b,Replay(),clock=lambda:100,trusted_ingress=fixture_ingress(pair[1]))
        wire=a.acquire().begin('pair','echo',canonical(payload()),100).wire()
        async def receive():
            if pause_at=='body':
                paused.set();await resume.wait()
            return {'type':'http.request','body':wire}
        async def send(message): sent.append(message)
        task=asyncio.create_task(wrapper({'type':'http','method':'POST','path':'/_app-security/v1/message',
            'headers':[(b'content-type',b'application/app-security+json')]},receive,send))
        await asyncio.wait_for(paused.wait(),2)
        b.revoke(pair[2]['app_instance_id'],1)
        resume.set()
        await asyncio.wait_for(task,2)
        assert not seen and sent[0]['status']==503
        await wrapper.jobs.close()
    asyncio.run(run())
