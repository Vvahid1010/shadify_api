"""Real signatures and producer/receiver; native peer metadata is synthetic."""
import asyncio
import hashlib
from datetime import timedelta
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from app_security_shell.transport import TrustedPeer
from pydantic import SecretStr
from shadify_api.identity_sync import ProjectionReceiver
from shadify_api.identity_sync import canonical
DATA=dict(schema_version=1,account_id='11111111-1111-4111-8111-111111111111',domain_app='shadify',factor_scope_id='22222222-2222-4222-8222-222222222222',proxy_app='shadify_api',version=1,identity_status='active',verified_email='test@example.invalid',verified_phone='+989121234567')


def fixture():
    sender=dict(node_id=str(uuid4()),app_id='authentication',app_instance_id=str(uuid4()),generation=1)
    recipient=dict(node_id=str(uuid4()),app_id='shadify_api',app_instance_id=str(uuid4()),generation=1)
    row=dict(connection_id=str(uuid4()),policy_revision=3,sender={'identity':sender},recipient={'identity':recipient},endpoint_ids=['shadify_api.identity_sync.apply'])
    scopes={'shadify':DATA['factor_scope_id']}
    secret='synthetic-legacy-proof-key'*4
    settings=NS(api_prefix='/api',auth_assertion_site_scopes=scopes,auth_assertion_keys={'test.1':SecretStr(secret)},
        app_security={'contract_version':1,'extra_encryption_default':True,'credentials_file':'app_security.credentials.json','credential_snapshot_sha256':'a'*64,'endpoint_overrides':[],'local':recipient,'connections':[row]})
    revision=(row['connection_id'],3,'d'*64)
    def outbound_only(cid):
        raise AssertionError('receiver_must_not_use_outbound_transport_binding')
    native=NS(revisions=lambda:[revision],transport_binding=outbound_only)
    binding=NS(acquire=lambda:native)
    verifier=ProjectionReceiver(binding,settings.app_security,settings.auth_assertion_site_scopes)
    auth=NS(domain_apps={k:NS(factor_scope_id=v,proxy_app='shadify_api') for k,v in scopes.items()},
        state_token_ttl_minutes=1,proxy_apps={'shadify_api':NS(base_url='https://fixture.invalid',path_prefix='/api')},
        auth_assertion_current_version='test.1',resolved_auth_assertion_keys={'test.1':SecretStr(secret)})
    peer=TrustedPeer(row['connection_id'],sender['node_id'],sender['app_instance_id'])
    return auth,settings,verifier,peer,row,secret


def proof(auth,row,secret,data,**changes):
    claims=dict(purpose='identity-projection-v1',account_id=data['account_id'],domain_app=data['domain_app'],proxy_app=data['proxy_app'],
        connection_id=row['connection_id'],connection_revision=row['policy_revision'],sender_node_id=row['sender']['identity']['node_id'],
        receiver_node_id=row['recipient']['identity']['node_id'],projection_version=data['version'],payload_sha256=hashlib.sha256(canonical(data)).hexdigest())
    claims.update(changes)
    # Synthetic signer uses the existing state-token byte contract; this is not a kernel/producer integration test.
    import time,base64,json,hmac
    claims['factor_scope_id']=auth.domain_apps[data['domain_app']].factor_scope_id
    claims.update(iat=int(time.time()),exp=int(time.time())+60)
    raw=json.dumps(claims,separators=(',',':')).encode()
    encode=lambda x:base64.urlsafe_b64encode(x).decode().rstrip('=')
    return 'test.1.'+encode(raw)+'.'+encode(hmac.new(hashlib.sha256(('state-token:'+secret).encode()).digest(),raw,hashlib.sha256).digest())


@pytest.mark.parametrize('site',['shadify'])
@pytest.mark.parametrize('fault',[None,'body','scope','site','peer','connection','revision','purpose','extra','expired','policy'])
def test_legacy_proof_exact_native_site_binding(site,fault,monkeypatch):
    auth,settings,verifier,peer,row,secret=fixture()
    data=DATA|{'domain_app':site,'factor_scope_id':settings.auth_assertion_site_scopes[site]}
    changes={}
    if fault=='connection':changes['connection_id']=str(uuid4())
    if fault=='revision':changes['connection_revision']=4
    if fault=='purpose':changes['purpose']='auth-assertion'
    if fault=='extra':changes['session_id']='not-service'
    token=proof(auth,row,secret,data,**changes)
    if fault=='body':data=data|{'verified_email':'different@example.invalid'}
    if fault=='scope':data=data|{'factor_scope_id':str(uuid4())}
    if fault=='site':data=data|{'domain_app':'other_site' if site=='shadify' else 'shadify'}
    if fault=='peer':peer=TrustedPeer(peer.connection_id,'wrong-node',peer.app_instance_id)
    if fault=='expired':monkeypatch.setattr('shadify_api.identity_sync.time.time',lambda:4000000000)
    if fault=='policy':verifier.binding.acquire().revisions=lambda:[]
    call=lambda:verifier.verify(token,peer,data,method='POST',target='/api/v1/identity-sync')
    if fault is None:call()
    else:
        with pytest.raises(ValueError):call()


def test_invalid_scope_and_provenance_is_not_granted():
    from shadify_api.identity_sync import validate_scopes
    for value in (None,{'other':DATA['factor_scope_id']},{'shadify':'bad'}):
        with pytest.raises(ValueError):validate_scopes(value)
    from shadify_api.admin import active_identity
    assert not active_identity({'active':True,'first_admitted_at':None,'admission_source':None})
