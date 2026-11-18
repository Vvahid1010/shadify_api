"""Mode admission, trusted OFF transport, and policy agreement without services."""
import asyncio
import copy
import json
from dataclasses import replace

import httpx
import pytest

from app_security_shell._native import NativeContext
from app_security_shell.jobs import NativeJobs
from app_security_shell.lifecycle import ContextRegistry
from app_security_shell.transport import (
    Client, SyncClient, Middleware, POLICY_HEADER, TransportFailure, TrustedPeer,
)
from test_pair import pair, canonical, identity, payload, FixtureReplay, fixture_egress


def keyless_config(pair):
    directory, _, _, _, common, catalog = pair[0].inputs
    config = copy.deepcopy(common)
    config.update(local=identity(pair[1]), credential_snapshot_sha256='')
    config['endpoint_overrides'] = [dict(connection_id='pair', endpoint_id='echo', extra_encryption=False)]
    for peer in ('sender', 'recipient'):
        config['connections'][0][peer].pop('keys')
    return str(directory/'absent'), config, copy.deepcopy(catalog)


def test_off_only_and_empty_enrollment_never_read_credentials(pair):
    directory, cfg, catalog = keyless_config(pair)
    native = NativeContext(directory, canonical(cfg), canonical(catalog))
    assert native.route('pair', 'POST', '/echo') == ('echo', False)
    with pytest.raises(ValueError): native.begin('pair', 'echo', canonical(payload()), 100)
    # There is no credential path to read, including for a valid unenrolled identity.
    cfg.update(connections=[], endpoint_overrides=[])
    native = NativeContext(directory, canonical(cfg), canonical(catalog))
    assert native.revisions() == []
    with pytest.raises(ValueError): native.route('pair', 'POST', '/echo')


@pytest.mark.parametrize('change', ['default_on', 'mixed', 'unknown_override', 'duplicate_override', 'bad_identity', 'bad_revision', 'unsupported_off'])
def test_keyless_does_not_weaken_admission(pair, change):
    directory, cfg, catalog = keyless_config(pair)
    if change == 'default_on': cfg['endpoint_overrides'] = []
    elif change == 'mixed':
        catalog.append(dict(catalog[0], endpoint_id='other', path='/other'))
        cfg['connections'][0]['endpoint_ids'].append('other')
    elif change == 'unknown_override': cfg['endpoint_overrides'][0]['endpoint_id'] = 'unknown'
    elif change == 'duplicate_override': cfg['endpoint_overrides'] *= 2
    elif change == 'bad_identity': cfg['local']['app_instance_id'] = 'invented'
    elif change == 'bad_revision': cfg['connections'][0]['policy_revision'] = 0
    elif change == 'unsupported_off': catalog[0]['off_supported'] = False
    with pytest.raises(ValueError): NativeContext(directory, canonical(cfg), canonical(catalog))


def test_off_to_on_requires_new_revision_and_real_material(pair):
    directory, cfg, catalog = keyless_config(pair)
    registry = ContextRegistry()
    registry.install(registry.prepare(directory, canonical(cfg), canonical(catalog)))
    original = registry.acquire()
    realdir, _, digest, _, common, _ = pair[0].inputs
    on = copy.deepcopy(common) | dict(local=identity(pair[1]), credential_snapshot_sha256=digest)
    # Same revision cannot alter OFF policy, even with valid keys.
    with pytest.raises(ValueError): registry.prepare(str(realdir), canonical(on), canonical(catalog))
    on['connections'][0]['policy_revision'] = 2
    with pytest.raises(ValueError): registry.prepare(directory, canonical(on), canonical(catalog))
    assert registry.acquire() is original
    registry.install(registry.prepare(str(realdir), canonical(on), canonical(catalog)))
    assert registry.acquire().route('pair','POST','/echo') == ('echo', True)
    with pytest.raises(ValueError): registry.prepare(directory, canonical(cfg), canonical(catalog))
    registry.revoke(pair[1]['app_instance_id'],1)
    with pytest.raises(ValueError): registry.acquire()


@pytest.mark.parametrize('synchronous', [False, True])
@pytest.mark.parametrize('fault', ['missing', 'destination', 'identity', 'client', 'policy'])
def test_off_egress_requires_exact_attestation_before_io(pair, synchronous, fault):
    a,_ = pair[0](True)
    sends=[]
    def handler(request):
        sends.append(request)
        raise AssertionError('must not send')
    def adapter(connection, base, http):
        value=fixture_egress(a)(connection,base,http)
        return replace(value, **{
            'destination':dict(base_url='http://wrong'),
            'identity':dict(recipient_app_instance_id='wrong'),
            'client':dict(http_client=object()),
            'policy':dict(policy_revision=99),
        }[fault])
    async def run():
        jobs=NativeJobs()
        try:
            hook=None if fault=='missing' else adapter
            if synchronous:
                with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                    with pytest.raises(TransportFailure) as err:
                        SyncClient(a,http,jobs=jobs,trusted_egress=hook).post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            else:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    with pytest.raises(TransportFailure) as err:
                        await Client(a,http,jobs=jobs,trusted_egress=hook).post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            assert err.value.outcome=='not_sent' and not sends
        finally: await jobs.close()
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['missing', 'revision', 'digest', 'endpoint', 'duplicate', 'identity', 'on_receiver'])
def test_off_request_disagreement_never_dispatches(pair, fault):
    a,b=pair[0](True)
    if fault=='on_receiver': _,b=pair[0]()
    metadata=a.plain_policy('pair','echo')
    parsed=json.loads(metadata)
    if fault=='revision': parsed[3]+=1
    elif fault=='digest': parsed[4]='0'*64
    elif fault=='endpoint': parsed[2]='wrong'
    headers=[(POLICY_HEADER,canonical(parsed))]
    if fault=='missing': headers=[]
    elif fault=='duplicate': headers*=2
    calls=[]
    async def app(scope,receive,send): calls.append(1)
    async def run():
        peer=TrustedPeer('pair',pair[1]['node_id'],pair[1]['app_instance_id'])
        if fault=='identity': peer=replace(peer,app_instance_id='wrong')
        middleware=Middleware(app,b,FixtureReplay(),trusted_ingress=lambda scope:peer)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware)) as http:
                response=await http.post('http://fixture/echo',json={},headers=headers)
                assert response.status_code==503 and not calls
        finally: await middleware.jobs.close()
    asyncio.run(run())


@pytest.mark.parametrize('synchronous', [False, True])
@pytest.mark.parametrize('fault', ['missing', 'revision', 'digest', 'endpoint', 'duplicate'])
def test_off_bad_response_is_unknown_and_never_retried(pair, synchronous, fault):
    a,_=pair[0](True)
    sends=[]
    def handler(request):
        sends.append(request)
        metadata=json.loads(request.headers[POLICY_HEADER.decode()])
        if fault=='revision': metadata[3]+=1
        elif fault=='digest': metadata[4]='0'*64
        elif fault=='endpoint': metadata[2]='wrong'
        headers=[(POLICY_HEADER,canonical(metadata))]
        if fault=='missing': headers=[]
        elif fault=='duplicate': headers*=2
        return httpx.Response(200,headers=headers,stream=httpx.ByteStream(b'{}'))
    async def run():
        jobs=NativeJobs()
        try:
            if synchronous:
                with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                    with pytest.raises(TransportFailure) as err:
                        SyncClient(a,http,jobs=jobs,trusted_egress=fixture_egress(a)).post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            else:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    with pytest.raises(TransportFailure) as err:
                        await Client(a,http,jobs=jobs,trusted_egress=fixture_egress(a)).post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            assert err.value.outcome=='outcome_unknown' and len(sends)==1
        finally: await jobs.close()
    asyncio.run(run())


@pytest.mark.parametrize('synchronous', [False, True])
def test_off_server_headers_are_transport_metadata(pair, synchronous):
    a,_=pair[0](True)
    sends=[]
    def handler(request):
        sends.append(request)
        return httpx.Response(422, headers={POLICY_HEADER.decode():request.headers[POLICY_HEADER.decode()],
            'content-type':'application/json','date':'Sun, 20 Sep 2026 12:00:00 GMT','server':'fixture-proxy'},
            stream=httpx.ByteStream(b'{"error":"business"}'))
    async def run():
        jobs=NativeJobs()
        try:
            if synchronous:
                with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                    result=SyncClient(a,http,jobs=jobs,trusted_egress=fixture_egress(a)).post_json(
                        connection='pair',base_url='http://fixture',path='/echo',json_body={})
            else:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    result=await Client(a,http,jobs=jobs,trusted_egress=fixture_egress(a)).post_json(
                        connection='pair',base_url='http://fixture',path='/echo',json_body={})
            assert result.status_code==422 and result.json()=={'error':'business'}
            assert len(sends)==1
            assert 'date' not in result.headers and 'server' not in result.headers
            assert POLICY_HEADER.decode() not in result.headers
        finally: await jobs.close()
    asyncio.run(run())


def test_mixed_connections_only_require_keys_for_on_connection(pair):
    directory, _, digest, _, common, catalog = pair[0].inputs
    cfg = copy.deepcopy(common) | dict(local=identity(pair[1]), credential_snapshot_sha256=digest)
    off = copy.deepcopy(cfg['connections'][0])
    off['connection_id'] = 'off-peer'
    for peer in ('sender','recipient'): off[peer].pop('keys')
    cfg['connections'].append(off)
    cfg['endpoint_overrides'] = [dict(connection_id='off-peer', endpoint_id='echo', extra_encryption=False)]
    native=NativeContext(str(directory),canonical(cfg),canonical(catalog))
    assert native.route('pair','POST','/echo')==('echo',True)
    assert native.route('off-peer','POST','/echo')==('echo',False)
    cfg['connections'][0]['recipient'].pop('keys')
    with pytest.raises(ValueError): NativeContext(str(directory),canonical(cfg),canonical(catalog))


def test_off_valid_policy_header_cannot_supply_transport_identity(pair):
    a,b=pair[0](True)
    calls=[]
    async def app(scope,receive,send): calls.append(1)
    async def run():
        wrapper=Middleware(app,b,FixtureReplay())
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapper)) as http:
                response=await http.post('http://fixture/echo',json={},headers={
                    POLICY_HEADER.decode():a.plain_policy('pair','echo'),
                    'x-app-security-peer':'wallet_manager','x-cell-id':pair[1]['node_id']})
                assert response.status_code==503 and not calls
        finally: await wrapper.jobs.close()
    asyncio.run(run())


@pytest.mark.parametrize('synchronous',[False,True])
def test_off_revoked_during_egress_attestation_never_sends(pair,synchronous):
    a,_=pair[0](True,registries=True)
    sent=[]
    def hook(connection,base,http):
        evidence=fixture_egress(a)(connection,base,http)
        a.revoke(pair[1]['app_instance_id'],1)
        return evidence
    def handler(request):
        sent.append(request)
        raise AssertionError('must not send')
    async def run():
        jobs=NativeJobs()
        try:
            if synchronous:
                with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                    with pytest.raises(TransportFailure) as err:
                        SyncClient(a,http,jobs=jobs,trusted_egress=hook).post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            else:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    with pytest.raises(TransportFailure) as err:
                        await Client(a,http,jobs=jobs,trusted_egress=hook).post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            assert err.value.outcome=='not_sent' and not sent
        finally: await jobs.close()
    asyncio.run(run())
