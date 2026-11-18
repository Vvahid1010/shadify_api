"""Compiled contexts, independent transcript verification, and real HTTP adapters."""
import asyncio
import base64
import copy
import hashlib
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pqcrypto.kem import ml_kem_768
from pqcrypto.sign import ml_dsa_65

from app_security_shell._native import NativeContext
from app_security_shell.transport import Client, Middleware, TransportFailure, TrustedPeer, TrustedEgress
from test_key_admission import material as material_fixture, write_material


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def raw(pem):
    return base64.b64decode(''.join(pem.splitlines()[1:-1]))


def identity(material):
    return {k: material[k] for k in ('node_id', 'app_id', 'app_instance_id', 'generation')}


def peer(material):
    return {'identity': identity(material), 'keys': {
        'key_ids': {k: material[k]['kid'] for k in ('ed25519','ml_dsa_65','ml_kem_768')},
        'ed25519': base64.b64encode(serialization.load_pem_public_key(material['ed25519']['public_key_pem'].encode()).public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode(),
        'ml_dsa_65': base64.b64encode(raw(material['ml_dsa_65']['public_key_pem'])).decode(),
        'ml_kem_768': base64.b64encode(raw(material['ml_kem_768']['public_key_pem'])).decode(),
    }}


@pytest.fixture
def pair(tmp_path):
    alice, bob = material_fixture.__wrapped__(), material_fixture.__wrapped__()
    bob.update(node_id='44444444-4444-4444-8444-444444444444', app_id='signatory',
        app_instance_id='55555555-5555-4555-8555-555555555555', owner_app_instance_id='55555555-5555-4555-8555-555555555555')
    alice_dir, bob_dir = tmp_path / 'alice', tmp_path / 'bob'
    _, alice_hash = write_material(alice_dir, alice)
    _, bob_hash = write_material(bob_dir, bob)
    catalog = [{'endpoint_id':'echo', 'receiver_app':'signatory', 'permitted_peer_apps':['wallet_manager'],
                'method':'POST','path':'/echo','headers':['accept','content-type','x-cell-id','x-signatory-trace-id'], 'off_supported':True}]
    conn = {'connection_id':'pair','policy_revision':1,'sender':peer(alice),'recipient':peer(bob),'endpoint_ids':['echo']}
    common = {'contract_version':1,'extra_encryption_default':True,'credentials_file':'app_security.credentials.json',
              'connections':[conn],'endpoint_overrides':[]}
    def build(off=False, registries=False):
        extra={'endpoint_overrides':[{'connection_id':'pair','endpoint_id':'echo','extra_encryption':False}]} if off else {}
        def context(directory, material, digest):
            config=canonical(common | {'local':identity(material),'credential_snapshot_sha256':digest} | extra)
            if registries:
                from app_security_shell.lifecycle import ContextRegistry
                registry=ContextRegistry()
                registry.install(registry.prepare(str(directory),config,canonical(catalog)))
                return registry
            return NativeContext(str(directory),config,canonical(catalog))
        a=context(alice_dir,alice,alice_hash)
        b=context(bob_dir,bob,bob_hash)
        return a,b
    build.inputs=(alice_dir,bob_dir,alice_hash,bob_hash,common,catalog)
    return build, alice,bob


def fixture_ingress(material, connection='pair'):
    return lambda scope: TrustedPeer(connection, material['node_id'], material['app_instance_id'])


def fixture_egress(native):
    """Synthetic channel evidence for isolated HTTP/ASGI test transports only."""
    def attest(connection, base_url, http_client):
        selected = native.acquire() if hasattr(native, 'acquire') else native
        return TrustedEgress(connection, base_url, *selected.transport_binding(connection), http_client)
    return attest


def payload(body=b'{"secret":"synthetic"}', status=None, content_type=b'application/json'):
    return {'method':'POST','path':'/echo','query_b64':'','headers':[{'name':'content-type','value_b64':base64.b64encode(content_type).decode()}],
            'body_b64':base64.b64encode(body).decode(),'status':status,'response_to':None}


def test_independent_provider_verifies_and_decrypts(pair):
    build,alice,bob=pair
    a,b=build()
    ticket=a.begin('pair','echo',canonical(payload()),100)
    wire=json.loads(ticket.wire())
    data=b'app-security-v1/sign\0'+canonical({'meta':wire['meta'],'enc':wire['enc']})
    serialization.load_pem_public_key(alice['ed25519']['public_key_pem'].encode()).verify(base64.b64decode(wire['signatures']['ed25519']),data)
    assert ml_dsa_65.verify(raw(alice['ml_dsa_65']['public_key_pem']),data,base64.b64decode(wire['signatures']['ml_dsa_65']))
    enc=wire['enc']
    secret=ml_kem_768.decrypt(raw(bob['ml_kem_768']['private_key_pem']),base64.b64decode(enc['kem_ciphertext']))
    aad=b'app-security-v1/aad\0'+canonical(wire['meta'])
    key=HKDF(algorithm=hashes.SHA256(),length=32,salt=base64.b64decode(enc['kdf_salt']),info=b'app-security-v1/aead\0'+hashlib.sha256(aad).digest()).derive(secret)
    assert json.loads(AESGCM(key).decrypt(base64.b64decode(enc['nonce']),base64.b64decode(enc['ciphertext']),aad)) == payload()
    received=b.receive(ticket.wire(),100)
    response=received.respond(canonical(payload(b'{"ok":true}',422)),180)
    assert json.loads(ticket.complete(response,180))['status'] == 422
    with pytest.raises(ValueError): ticket.complete(response,180)
    with pytest.raises(ValueError): received.respond(canonical(payload(b'',204)),180)


@pytest.mark.parametrize('field',['ed25519','ml_dsa_65'])
def test_both_signatures_required(pair,field):
    build,_,_=pair
    a,b=build()
    wire=json.loads(a.begin('pair','echo',canonical(payload()),100).wire())
    signature=bytearray(base64.b64decode(wire['signatures'][field])); signature[-1]^=1
    wire['signatures'][field]=base64.b64encode(signature).decode()
    with pytest.raises(ValueError): b.receive(canonical(wire),100)


def test_swapped_response_and_expiry(pair):
    build,_,_=pair
    a,b=build()
    one=a.begin('pair','echo',canonical(payload()),100)
    two=a.begin('pair','echo',canonical(payload()),100)
    response=b.receive(two.wire(),100).respond(canonical(payload(b'',204)),100)
    with pytest.raises(ValueError): one.complete(response,100)
    with pytest.raises(ValueError): b.receive(two.wire(),160)
    with pytest.raises(ValueError): b.receive(two.wire(),69)


@pytest.mark.parametrize('content_type',[b'Multipart/Form-Data; boundary=x',b' Text/Event-Stream ; charset=utf-8',b'multipart/mixed'])
def test_streaming_types_rejected(pair,content_type):
    a,_=pair[0]()
    with pytest.raises(ValueError): a.begin('pair','echo',canonical(payload(content_type=content_type)),100)


class FixtureReplay:
    """Only adapter unit tests; real Redis evidence is a separate suite."""
    def __init__(self): self.seen=set()
    async def reserve(self,digest,expiry,now):
        if digest in self.seen: raise ValueError('duplicate')
        self.seen.add(digest)


def test_actual_http_adapter_roundtrip(pair):
    async def run():
        a,b=pair[0]()
        seen=[]
        async def app(scope,receive,send):
            msg=await receive()
            seen.append((scope['path'],json.loads(msg['body'])))
            await send({'type':'http.response.start','status':422,'headers':[(b'content-type',b'application/json')]})
            await send({'type':'http.response.body','body':b'{"business":"error"}'})
        wrapper=Middleware(app,b,FixtureReplay(),trusted_ingress=fixture_ingress(pair[1]))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapper),trust_env=False) as http:
            client=Client(a,http,trusted_egress=fixture_egress(a))
            result=await client.post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={'unchanged':1})
            assert result.status_code==422 and result.json()=={'business':'error'}
            assert seen==[('/echo',{'unchanged':1})]
            assert str(result.request.url)=='http://fixture/echo'
            with pytest.raises(httpx.HTTPStatusError): result.raise_for_status()
            ordinary=await http.post('http://fixture/echo',json={})
            assert ordinary.status_code==503 and len(seen)==1
            await client.jobs.close()
        await wrapper.jobs.close()
    asyncio.run(run())


def test_plaintext_success_never_accepted(pair):
    async def run():
        a,_=pair[0]()
        async def handler(request): return httpx.Response(200,json={'raw_hex':'untrusted'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client=Client(a,http,trusted_egress=fixture_egress(a))
            with pytest.raises(TransportFailure) as e:
                await client.post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            assert e.value.outcome=='outcome_unknown'
            await client.jobs.close()
    asyncio.run(run())


def test_off_needs_trusted_identity(pair):
    async def run():
        a,b=pair[0](True)
        called=[]
        async def app(scope,receive,send):
            called.append(1)
            await send({'type':'http.response.start','status':204,'headers':[]})
            await send({'type':'http.response.body','body':b''})
        middleware=Middleware(app,b,FixtureReplay())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware)) as http:
            result=await http.post('http://fixture/echo',json={},headers={'x-app-security-peer':'wallet_manager'})
            assert result.status_code==503 and not called
            middleware.trusted_ingress=lambda scope:TrustedPeer('pair',pair[1]['node_id'],pair[1]['app_instance_id'])
            client=Client(a,http,trusted_egress=fixture_egress(a))
            result=await client.post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            assert result.status_code==204 and called==[1]
            await client.jobs.close()
        await middleware.jobs.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',['node','instance','connection','generation','policy','key_id','path','body','ciphertext','tag','extra','duplicate','version'])
def test_tampered_envelope_rejected(pair,mutation):
    a,b=pair[0]()
    wire=json.loads(a.begin('pair','echo',canonical(payload()),100).wire())
    if mutation=='node': wire['meta']['sender']['node_id']='99999999-9999-4999-8999-999999999999'
    elif mutation=='instance': wire['meta']['sender']['app_instance_id']='99999999-9999-4999-8999-999999999999'
    elif mutation=='connection': wire['meta']['connection_id']='other'
    elif mutation=='generation': wire['meta']['sender']['generation']+=1
    elif mutation=='policy': wire['meta']['policy_revision']+=1
    elif mutation=='key_id': wire['meta']['key_ids']['ed25519']='other'
    elif mutation=='path': wire['meta']['endpoint_id']='admin'
    elif mutation=='body': wire['enc']['ciphertext']='AA=='
    elif mutation in ('ciphertext','tag'):
        value=bytearray(base64.b64decode(wire['enc']['ciphertext']));value[0 if mutation=='ciphertext' else -1]^=1
        wire['enc']['ciphertext']=base64.b64encode(value).decode()
    elif mutation=='extra': wire['unexpected']=True
    elif mutation=='version': wire['meta']['version']=2
    encoded=canonical(wire)
    if mutation=='duplicate': encoded=encoded.replace(b'"version":1',b'"version":1,"version":1')
    with pytest.raises(ValueError,match='^app_security_rejected$'): b.receive(encoded,100)


def test_body_limit_and_large_psbt_fixture(pair):
    from embit.psbt import PSBT
    from embit.transaction import Transaction,TransactionInput,TransactionOutput
    from embit.script import Script
    # Valid synthetic 1,000-input/1,000-output unsigned PSBT, no signing/broadcast.
    tx=Transaction(vin=[TransactionInput(i.to_bytes(32,'big'),0) for i in range(1000)],
        vout=[TransactionOutput(1000,Script(b'\x00\x14'+bytes(20))) for _ in range(1000)])
    psbt=PSBT(tx).serialize()
    assert PSBT.parse(psbt).serialize()==psbt
    body=canonical({'psbt':base64.b64encode(psbt).decode(),'utxos':[{'txid':i.to_bytes(32,'big').hex(),'vout':0,'value':2000} for i in range(1000)]})
    a,b=pair[0]()
    received=b.receive(a.begin('pair','echo',canonical(payload(body)),100).wire(),100)
    assert base64.b64decode(json.loads(received.payload())['body_b64'])==body
    # Exact byte limits apply to the opaque body, regardless of business format.
    body=b'x'*(1024*1024)
    ticket=a.begin('pair','echo',canonical(payload(body)),100)
    assert len(ticket.wire())<2*1024*1024
    assert base64.b64decode(json.loads(b.receive(ticket.wire(),100).payload())['body_b64'])==body
    with pytest.raises(ValueError): a.begin('pair','echo',canonical(payload(body+b'x')),100)


@pytest.mark.parametrize('synchronous',[False,True])
def test_off_rejects_compressed_response_before_decoding(pair,synchronous):
    from app_security_shell.jobs import NativeJobs
    from app_security_shell.transport import SyncClient
    a,_=pair[0](True)
    reads=[]
    class CountedStream(httpx.SyncByteStream,httpx.AsyncByteStream):
        def __iter__(self):
            reads.append(1)
            yield b'not-gzip'
        async def __aiter__(self):
            reads.append(1)
            yield b'not-gzip'
    def handler(request):
        # Deliberately not valid gzip: a decoder would raise before our gate.
        return httpx.Response(200,headers={'content-encoding':'gzip'},stream=CountedStream())
    async def run():
        jobs=NativeJobs()
        try:
            if synchronous:
                with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                    client=SyncClient(a,http,jobs=jobs,trusted_egress=fixture_egress(a))
                    with pytest.raises(TransportFailure) as err:
                        client.post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            else:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    client=Client(a,http,jobs=jobs,trusted_egress=fixture_egress(a))
                    with pytest.raises(TransportFailure) as err:
                        await client.post_json(connection='pair',base_url='http://fixture',path='/echo',json_body={})
            assert err.value.outcome=='outcome_unknown'
            assert not reads
        finally: await jobs.close()
    asyncio.run(run())
