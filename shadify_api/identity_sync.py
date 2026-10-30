"""Auth facts over canonical admitted transport; never artist/admin authority."""
import base64
import hashlib
import json
import time
from uuid import UUID, uuid4
from fastapi import HTTPException
from psycopg.rows import dict_row
from app_security_shell.transport import TrustedPeer
from .auth import unique_pairs


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def validate_scopes(value):
    if type(value) is not dict or set(value) - {'shadify'}:
        raise ValueError('identity_sync_scope_invalid')
    for scope in value.values():
        if type(scope) is not str or str(UUID(scope)) != scope:
            raise ValueError('identity_sync_scope_invalid')
    return value


class ProjectionReceiver:
    def __init__(self, binding, policy, scopes):
        self.binding, self.policy, self.scopes = binding, policy, validate_scopes(scopes)

    def verify(self, proof, admitted, value, *, method, target):
        # As for existing Shadify user assertions, canonical shell authenticates
        # complete request bytes. No second app-local signature/key registry.
        row,sender,recipient=self.connection(admitted)
        if type(proof) is not str or len(proof)>8192: raise ValueError('identity_sync_proof_rejected')
        import re
        version,encoded,signature=proof.rsplit('.',2)
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+',version) or
            not re.fullmatch(r'[A-Za-z0-9_-]+',encoded) or not re.fullmatch(r'[A-Za-z0-9_-]+',signature)):
            raise ValueError('identity_sync_proof_rejected')
        raw=base64.b64decode(encoded+'='*(-len(encoded)%4),altchars=b'-_',validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip('=')!=encoded: raise ValueError('identity_sync_proof_rejected')
        claims=json.loads(raw,object_pairs_hook=unique_pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        expected=dict(purpose='identity-projection-v1',account_id=value['account_id'],domain_app=value['domain_app'],
            factor_scope_id=value['factor_scope_id'],proxy_app=value['proxy_app'],connection_id=row['connection_id'],
            connection_revision=row['policy_revision'],sender_node_id=sender['node_id'],receiver_node_id=recipient['node_id'],
            projection_version=value['version'],payload_sha256=hashlib.sha256(canonical(value)).hexdigest())
        if (sender['app_id']!='authentication' or recipient!=self.policy['local'] or recipient['app_id']!='shadify_api'
            or value['proxy_app']!='shadify_api' or value['domain_app']!='shadify'
            or self.scopes.get(value['domain_app'])!=value['factor_scope_id']
            or 'shadify_api.identity_sync.apply' not in row['endpoint_ids']
            or method!='POST' or target!='/api/v1/identity-sync'
            or type(claims) is not dict or set(claims)!=set(expected)|{'iat','exp'}
            or any(type(claims.get(k)) is not type(v) or claims[k]!=v for k,v in expected.items())):
            raise ValueError('identity_sync_proof_rejected')
        issued,expires=claims['iat'],claims['exp'];now=int(time.time())
        if (type(issued) is not int or type(expires) is not int or expires<=issued or expires-issued>60
            or issued>now+5 or expires<=now):raise ValueError('identity_sync_proof_expired')
        return (str(recipient['node_id']),str(recipient['app_instance_id']))


    def connection(self, admitted):
        if isinstance(admitted, TrustedPeer):
            rows=[r for r in self.policy['connections'] if r['connection_id']==admitted.connection_id
                  and (r['sender']['identity']['node_id'],r['sender']['identity']['app_instance_id'])
                  ==(admitted.node_id,admitted.app_instance_id)]
        else:
            from app_security_shell._native import InboundRequest
            if not isinstance(admitted, InboundRequest):
                raise ValueError('identity_sync_admission_required')
            rows=[]
            for r in self.policy['connections']:
                peer=r['sender']['identity']
                try: admitted.admit_transport(r['connection_id'],peer['node_id'],peer['app_instance_id'])
                except ValueError: continue
                rows.append(r)
        if len(rows)!=1: raise ValueError('identity_sync_peer_rejected')
        row=rows[0];sender=row['sender']['identity'];recipient=row['recipient']['identity']
        if ((row['connection_id'],row['policy_revision']) not in
                [tuple(x[:2]) for x in self.binding.acquire().revisions()]):
            raise ValueError('identity_sync_policy_changed')
        if sender['app_id']!='authentication' or recipient!=self.policy['local'] or recipient['app_id']!='shadify_api':
            raise ValueError('identity_sync_peer_rejected')
        return row,sender,recipient

    def current_recipient(self, admitted):
        _,_,recipient=self.connection(admitted)
        return (str(recipient['node_id']),str(recipient['app_instance_id']))


def apply_projection(connection, payload, *, installation=None):
    from .primary_selection import lock_scope, reconcile
    value=payload.model_dump();digest=hashlib.sha256(canonical(value)).hexdigest()
    with connection.cursor(row_factory=dict_row) as c:
        state=lock_scope(c)
        c.execute('INSERT INTO shadify_users(id,auth_account_id) VALUES (%s,%s) ON CONFLICT(auth_account_id) DO NOTHING',
                  (uuid4(),payload.account_id))
        user=c.execute('SELECT id FROM shadify_users WHERE auth_account_id=%s FOR UPDATE',(payload.account_id,)).fetchone()
        row=c.execute('SELECT * FROM auth_identity_projections WHERE user_id=%s AND domain_app=%s AND factor_scope_id=%s FOR UPDATE',
                      (user['id'],payload.domain_app,payload.factor_scope_id)).fetchone()
        if row and row['version']==payload.version and row['payload_sha256']!=digest:
            raise HTTPException(409,'identity_sync_version_conflict')
        if row is None or row['version']<payload.version:
            email=payload.verified_email if payload.identity_status=='active' else None
            phone=payload.verified_phone if payload.identity_status=='active' else None
            def contact(kind,value):return hashlib.sha256(canonical([kind,payload.domain_app,payload.factor_scope_id,value])).hexdigest() if value else None
            c.execute('INSERT INTO auth_identity_projections(user_id,domain_app,factor_scope_id,version,payload_sha256,identity_status,email_digest,phone_digest,email_masked,phone_masked) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,domain_app,factor_scope_id) DO UPDATE SET '
                'version=EXCLUDED.version,payload_sha256=EXCLUDED.payload_sha256,identity_status=EXCLUDED.identity_status,'
                'email_digest=EXCLUDED.email_digest,phone_digest=EXCLUDED.phone_digest,email_masked=EXCLUDED.email_masked,phone_masked=EXCLUDED.phone_masked',
                (user['id'],payload.domain_app,payload.factor_scope_id,payload.version,digest,payload.identity_status,
                 contact('email',email),contact('phone',phone),email[0]+'***@'+email.split('@')[1] if email else None,
                 '+'+'*'*(len(phone)-5)+phone[-4:] if phone else None))
        reconcile(c, factor_scope_id=payload.factor_scope_id, state=state, installation=installation)
    # Projection never manufactures admission/profile/roles. An independently
    # authorized pending primary selection may resolve against these verified facts.
    return {k:value[k] for k in ('account_id','domain_app','factor_scope_id','proxy_app','version')}|{'payload_sha256':digest}


def install_routes(app, repository, receiver):
    from fastapi import Request
    from .identity_sync_schema import IdentitySyncInput
    @app.post('/api/v1/identity-sync')
    async def receive(payload: IdentitySyncInput, request: Request):
        if repository is None or receiver is None:raise HTTPException(503,'identity_sync_unavailable')
        headers=request.scope.get('headers',[])
        proofs=[v for k,v in headers if k.lower()==b'x-auth-identity-projection']
        if len(proofs)!=1 or any(k.lower()==b'x-auth-assertion' or k.lower().startswith(b'x-authenticated-') for k,v in headers):
            raise HTTPException(403,'identity_sync_proof_required')
        try:
            if (request.scope.get('query_string',b'') or request.scope.get('raw_path')!=b'/api/v1/identity-sync'
                or await request.body()!=canonical(payload.model_dump())):raise ValueError()
            proof=proofs[0].decode('ascii');admitted=request.scope.get('app_security.admitted')
            def verify():return receiver.verify(proof,admitted,payload.model_dump(),method=request.method,target=request.url.path)
            installation=verify()
        except (ValueError,UnicodeError):raise HTTPException(403,'identity_sync_proof_rejected') from None
        def transaction():
            with repository.connect() as c:
                result=apply_projection(c,payload,installation=installation)
                try:verify()
                except (ValueError,UnicodeError):raise HTTPException(403,'identity_sync_proof_rejected') from None
                return result
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(transaction)
