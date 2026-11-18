"""Independent channel identity is mandatory in both envelope modes."""
import asyncio
from dataclasses import replace

import httpx
import pytest
from app_security_shell.jobs import NativeJobs
from app_security_shell.transport import (Client, SyncClient, Middleware, TrustedPeer,
    TransportFailure, POLICY_HEADER, MEDIA, CARRIER)
from test_pair import pair, canonical, payload, FixtureReplay, fixture_egress, fixture_ingress


@pytest.mark.parametrize('off', [False, True])
@pytest.mark.parametrize('synchronous', [False, True])
@pytest.mark.parametrize('fault', ['missing', 'type', 'connection', 'sender_node', 'sender_instance',
                                  'recipient_node', 'recipient_instance', 'revision', 'digest', 'client', 'base'])
def test_channel_egress_denies_before_io(pair, off, synchronous, fault):
    sender, _ = pair[0](off)
    sends = []
    def handler(request): sends.append(request); return httpx.Response(200)
    def hook(connection, base, client):
        if fault == 'type': return True
        proof = fixture_egress(sender)(connection, base, client)
        changes = {
            'connection': {'connection_id': 'wrong'}, 'sender_node': {'sender_node_id': 'wrong'},
            'sender_instance': {'sender_app_instance_id': 'wrong'},
            'recipient_node': {'recipient_node_id': 'wrong'},
            'recipient_instance': {'recipient_app_instance_id': 'wrong'},
            'revision': {'policy_revision': 99}, 'digest': {'policy_digest': '0' * 64},
            'client': {'http_client': object()}, 'base': {'base_url': 'http://wrong'},
        }
        return replace(proof, **changes[fault])
    async def run():
        jobs = NativeJobs()
        try:
            adapter = None if fault == 'missing' else hook
            if synchronous:
                with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                    with pytest.raises(TransportFailure) as error:
                        SyncClient(sender, http, jobs=jobs, trusted_egress=adapter).post_json(
                            connection='pair', base_url='http://fixture', path='/echo', json_body={})
            else:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    with pytest.raises(TransportFailure) as error:
                        await Client(sender, http, jobs=jobs, trusted_egress=adapter).post_json(
                            connection='pair', base_url='http://fixture', path='/echo', json_body={})
            assert error.value.outcome == 'not_sent' and sends == []
        finally: await jobs.close()
    asyncio.run(run())


@pytest.mark.parametrize('off', [False, True])
@pytest.mark.parametrize('fault', ['missing', 'type', 'connection', 'node', 'instance', 'valid'])
def test_channel_ingress_matches_authenticated_sender_before_replay(pair, off, fault):
    sender, receiver = pair[0](off)
    peer = fixture_ingress(pair[1])({})
    if fault in ('connection', 'node', 'instance'):
        peer = replace(peer, **{'connection': {'connection_id': 'wrong'},
                               'node': {'node_id': 'wrong'},
                               'instance': {'app_instance_id': 'wrong'}}[fault])
    elif fault == 'type': peer = True
    calls, reads, messages = [], [], []
    replay = FixtureReplay()
    async def business(scope, receive, send):
        calls.append(scope)
        await send(dict(type='http.response.start', status=204, headers=[]))
        await send(dict(type='http.response.body', body=b''))
    async def run():
        middleware = Middleware(business, receiver, replay, clock=lambda:100,
                               trusted_ingress=None if fault=='missing' else lambda scope:peer)
        if off:
            path, body = '/echo', b'{}'
            headers = [(b'content-type', b'application/json'), (POLICY_HEADER, sender.plain_policy('pair','echo').encode())]
        else:
            path = CARRIER
            body = sender.begin('pair','echo',canonical(payload()),100).wire()
            headers = [(b'content-type',MEDIA.encode())]
        async def receive(): reads.append(True); return dict(type='http.request',body=body)
        async def send(message): messages.append(message)
        try:
            await middleware(dict(type='http',method='POST',path=path,headers=headers),receive,send)
            if fault == 'valid':
                assert len(calls) == 1 and messages[0]['status'] == (204 if off else 200)
                assert len(replay.seen) == (0 if off else 1)
            else:
                assert not calls and not replay.seen and messages[0]['status'] == 503
                if fault in ('missing','type'): assert not reads
        finally: await middleware.jobs.close()
    asyncio.run(run())


def test_native_transport_comparison_does_not_consume_response(pair):
    sender, receiver = pair[0]()
    ticket = sender.begin('pair','echo',canonical(payload()),100)
    inbound = receiver.receive(ticket.wire(),100)
    for connection, node, instance in [('other',pair[1]['node_id'],pair[1]['app_instance_id']),
                                      ('pair','wrong',pair[1]['app_instance_id']),
                                      ('pair',pair[1]['node_id'],'wrong')]:
        with pytest.raises(ValueError): inbound.admit_transport(connection,node,instance)
    inbound.admit_transport('pair',pair[1]['node_id'],pair[1]['app_instance_id'])
    assert '204' in ticket.complete(inbound.respond(canonical(payload(b'',204)),100),100)


@pytest.mark.parametrize('off', [False, True])
@pytest.mark.parametrize('synchronous', [False, True])
def test_egress_revocation_during_attestation_cannot_send(pair,off,synchronous):
    sender, _ = pair[0](off,registries=True)
    sends=[]
    def hook(connection,base,client):
        proof=fixture_egress(sender)(connection,base,client)
        sender.revoke(pair[1]['app_instance_id'],1)
        return proof
    def channel(request): sends.append(request);return httpx.Response(200)
    async def run():
        jobs=NativeJobs()
        try:
            if synchronous:
                with httpx.Client(transport=httpx.MockTransport(channel)) as http:
                    with pytest.raises(TransportFailure) as error:
                        SyncClient(sender,http,jobs=jobs,trusted_egress=hook).post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            else:
                async with httpx.AsyncClient(transport=httpx.MockTransport(channel)) as http:
                    with pytest.raises(TransportFailure) as error:
                        await Client(sender,http,jobs=jobs,trusted_egress=hook).post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            assert error.value.outcome=='not_sent' and not sends
        finally: await jobs.close()
    asyncio.run(run())


@pytest.mark.parametrize('off', [False, True])
@pytest.mark.parametrize('change', ['revoke', 'replace'])
def test_ingress_context_change_in_hook_denies_before_body(pair, off, change):
    import copy
    from test_pair import identity
    sender, receiver = pair[0](off, registries=True)
    _, directory, _, digest, common, catalog = pair[0].inputs
    reads, calls, sent = [], [], []
    replay = FixtureReplay()
    def hook(scope):
        proof = fixture_ingress(pair[1])(scope)
        if change == 'revoke':
            receiver.revoke(pair[2]['app_instance_id'], 1)
        else:
            cfg = copy.deepcopy(common)
            cfg.update(local=identity(pair[2]), credential_snapshot_sha256=digest)
            cfg['connections'][0]['policy_revision'] = 2
            if off:
                cfg['endpoint_overrides'] = [dict(connection_id='pair', endpoint_id='echo', extra_encryption=False)]
            receiver.install(receiver.prepare(str(directory), canonical(cfg), canonical(catalog)))
        return proof
    async def run():
        async def business(scope, receive, send): calls.append(True)
        async def receive(): reads.append(True); return dict(type='http.request', body=b'')
        async def send(message): sent.append(message)
        middleware = Middleware(business, receiver, replay, trusted_ingress=hook)
        try:
            await middleware(dict(type='http', method='POST', path='/echo' if off else CARRIER,
                                  headers=[]), receive, send)
            assert not reads and not calls and not replay.seen
            assert sent[0]['status'] == 503
        finally: await middleware.jobs.close()
    asyncio.run(run())


def test_request_expiring_during_replay_cannot_reach_business(pair):
    sender, receiver = pair[0]()
    dispatched = []
    clock = [100]
    class DelayedReplay:
        async def reserve(self, *args):
            clock[0] = 160
    async def app(scope, receive, send):
        dispatched.append(scope)
        await send({'type': 'http.response.start', 'status': 204, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
    ticket = sender.begin('pair', 'echo', canonical(payload()), 100)
    async def run():
        jobs = NativeJobs()
        try:
            middleware = Middleware(app, receiver, DelayedReplay(), jobs=jobs,
                clock=lambda: clock[0], trusted_ingress=fixture_ingress(pair[1]))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware), base_url='http://fixture') as http:
                result = await http.post(CARRIER, content=ticket.wire(), headers={'content-type': MEDIA})
            assert result.status_code == 503
            assert dispatched == []
        finally:
            await jobs.close()
    asyncio.run(run())
