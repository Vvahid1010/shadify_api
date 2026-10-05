"""Real native ON admission + disposable SQL; synthetic Auth issuer/Node witness.
Kernel peer UID and Redis are fixtures. No actual Auth producer, popup or listener.
"""
import asyncio,base64,hashlib,json,socket,struct,tempfile,time
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock,patch
import httpx,uvicorn
from app_security_shell._native import NativeContext
from app_security_shell.replay import RedisReplayStore
from node_agent_local_shell_transport.local_uds import _HEADERS
from test_managed_ingress import canonical,material,identity,peer,FixtureRedis
import test_nomination_control as fixture
from test_artist_postgres import ArtistPostgresCase
TAG=fixture.TAG
from shadify_api.runtime import create_runtime_app
from shadify_api.repository import PostgresMediaRepository
from shadify_api.security import receiver_catalog

class NominationAdmissionTests(fixture.NominationControlCase):
    def test_native_on_register_context_claim_binding_and_size(self):asyncio.run(self.native_round_trip())
    async def native_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary,patch('node_agent_local_shell_transport.local_uds.pwd.getpwnam',return_value=Mock(pw_uid=1001)):
            root=Path(temporary);a=material('authentication');b=material('shadify_api');b['node_id']=a['node_id'];connection=str(uuid4())
            digests={}
            for value in (a,b):
                directory=root/value['app_id'];directory.mkdir(mode=0o700)
                content=canonical(value);p=directory/'app_security.credentials.json';p.write_bytes(content);p.chmod(0o600)
                digests[value['app_id']]=hashlib.sha256(content).hexdigest()
            common=dict(contract_version=1,extra_encryption_default=True,credentials_file='app_security.credentials.json',
                connections=[dict(connection_id=connection,policy_revision=1,sender=peer(a),recipient=peer(b),endpoint_ids=[e['endpoint_id'] for e in receiver_catalog()])],endpoint_overrides=[])
            ap=common|dict(local=identity(a),credential_snapshot_sha256=digests['authentication'])
            bp=common|dict(local=identity(b),credential_snapshot_sha256=digests['shadify_api'])
            sender=NativeContext(str(root/'authentication'),canonical(ap),canonical(receiver_catalog()))
            self.scope=self.scope.model_copy(update={'app_instance_id':b['app_instance_id']})
            ticks=[0.];redis=FixtureRedis()
            async def close():pass
            def factory(instance,clock,storage):return RedisReplayStore(redis,'app-security:replay:'+instance,clock,storage_bounds=storage,monotonic=lambda:ticks[0]),close
            bounds=Mock();bounds.clock_ready.return_value=True;bounds.storage_ready.return_value=True
            channel=dict(node_id=a['node_id'],sender_app_instance_id=a['app_instance_id'],recipient_app_instance_id=b['app_instance_id'],generation=1,record_sha256='a'*64,policy_revision=1,policy_digest=sender.transport_binding(connection)[5])
            config=dict(app_security=bp,app_security_binding=dict(schema_version=1,destinations=[],outbound_endpoints=[]),
                        app_security_transport=dict(profile='local_unix_v1',role='receiver',channels={connection:channel}),nomination_scope=self.scope.model_dump())
            directory=root/'shadify_api'
            self.reader=fixture.Reader(self.scope)
            def write(name,value):
                if name=='admin-nomination-witness.json':
                    from shadify_api.nomination_contract import Witness
                    self.reader.witness=Witness.model_validate(value)  # Explicit synthetic verified provider.
                p=directory/name;p.write_bytes(canonical(value));p.chmod(0o600)
            for name,value in {'shadify_api.config.json':config,'app_profile.json':{'schema_version':1,'app':'shadify_api'},
               'replay.credentials.json':{'url':'redis://:synthetic@127.0.0.1:6379/0'},
               'database.credentials.json':{'host':'127.0.0.1','dbname':'synthetic','user':'synthetic','password':'synthetic','sslmode':'disable'}}.items():write(name,value)
            repo=PostgresMediaRepository(self.connect)
            selected=[]
            composition=Mock()
            def bind_nomination(owner,scope_provider,receipt_connect):
                selected.append(scope_provider())  # Actual current Native ON receiver/instance.
                self.assertTrue(callable(receipt_connect))
                return self.reader  # Explicit synthetic provider; real UDS is separately tested.
            from shadify_api.nomination_transport import LazyNominationProvider
            composition.prepare.side_effect=lambda *args:LazyNominationProvider(lambda:bind_nomination(*args))
            with patch('shadify_api.replay_bounds.LocalReplayBounds',return_value=bounds),patch('shadify_api.runtime.replay_factory',return_value=factory),patch('shadify_api.runtime.PostgresMediaRepository',return_value=repo),patch('shadify_api.runtime.load_database_config',return_value=Mock(connect=self.connect,connect_nomination_receipt=self.connect)):app=create_runtime_app(directory,nomination_composition=composition)
            composition.prepare.assert_called_once()
            self.assertEqual(selected,[])
            binding=app.state.app_security;receiver=app.state.managed_receiver
            config=uvicorn.Config(app,log_config=None);config.load()
            protocol=receiver.http_protocol(config=config,server_state=__import__('uvicorn.server',fromlist=['ServerState']).ServerState(),app_state={})
            sock=Mock();sock.family=socket.AF_UNIX;sock.getsockopt.return_value=struct.pack('3i',1888,1001,1001)
            transport=Mock();transport.get_extra_info.side_effect=lambda name,default=None:sock if name=='socket' else default
            protocol.connection_made(transport)
            ingress={n.decode():v.decode() for n,v in zip(_HEADERS,receiver.channel.metadata,strict=True)}
            async def call(http,path,account='admin',raw=b'',extra=None,method='GET',token='synthetic-approval',claims_change=None,more_headers=None):
                now=int(time.time());claims=dict(purpose='auth-assertion',account_id=account,session_id='test-session',auth_level='mfa',domain_app='shadify',proxy_app='shadify_api',iat=now,exp=now+60)
                claims.update(extra or {});claims.update(claims_change or {})
                assertion='unit.'+base64.urlsafe_b64encode(canonical(claims)).decode().rstrip('=')+'.synthetic'
                headers={'x-auth-assertion':assertion,'x-authenticated-account-id':account,'x-authenticated-session-id':'test-session','x-authenticated-auth-level':'mfa','x-authenticated-domain-app':'shadify','x-authenticated-proxy-app':'shadify_api','content-type':'application/json'}
                if method=='POST':headers['x-proxy-approval']=token
                headers['x-request-id']=str(uuid4())
                headers.update(more_headers or {})
                body=dict(method=method,path=path,query_b64='',headers=[dict(name=k,value_b64=base64.b64encode(v.encode()).decode()) for k,v in headers.items()],body_b64=base64.b64encode(raw).decode(),status=None,response_to=None)
                endpoint='shadify_api.'+('admin.nomination' if method=='POST' else 'admin.nomination.context' if path.endswith('/context') else 'me.read')
                ticket=sender.begin(connection,endpoint,canonical(body),now)
                response=await http.post('http://fixture/_app-security/v1/message',content=ticket.wire(),headers={**ingress,'content-type':'application/app-security+json'})
                if response.status_code!=200:return response.status_code,{'error':response.text}
                decoded=json.loads(ticket.complete(response.content,int(time.time())))
                return decoded['status'],json.loads(base64.b64decode(decoded['body_b64']))
            try:
                await binding.ready()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(protocol.app)) as http:
                    self.assertEqual((await call(http,'/api/me'))[0],503);ticks[0]=152
                    context=await call(http,'/api/admin/nominations/context')
                    self.assertEqual(context[0],200,context);self.assertEqual(set(context[1]),{'account_id','session_id','scope'});self.assertIsNone(self.primary())
                    cmd=self.command();raw=cmd.model_dump_json().encode();digest=hashlib.sha256(raw).hexdigest();now=int(time.time())
                    projection=dict(scope=self.scope.model_dump(),operation='register',operation_id=cmd.operation_id,nomination_id=cmd.nomination_id,body_sha256=digest,selector_tags={'email':[TAG],'phone':[]},existing_account_id='owner-a')
                    approval=dict(approval_kind='proxy',mode='execute',account_id='admin',session_id='test-session',domain_app='shadify',scope='admin_nomination/v1',single_use=True,http_method='POST',proxy_path='/admin/nominations',proxy_query='',body_sha256=digest,jti=str(uuid4()),iat=now,exp=now+120)
                    evidence=dict(admin_nomination_v1=projection,proxy_approval=approval,proxy_approval_token_sha256=hashlib.sha256(b'synthetic-approval').hexdigest())
                    witness=dict(schema_version=1,scope=self.scope.model_dump(),operation='register',operation_id=cmd.operation_id,nomination_id=cmd.nomination_id,actor_account_id='admin',actor_session_id='test-session',body_sha256=digest,expires_at=now+120)
                    write('admin-nomination-witness.json',{**witness,'actor_session_id':'other'})
                    self.assertEqual((await call(http,'/api/admin/nominations',raw=raw,method='POST',extra=evidence))[0],401)
                    self.assertEqual(selected,[self.scope])
                    write('admin-nomination-witness.json',witness)
                    self.assertEqual((await call(http,'/api/admin/nominations',raw=raw+b' ',method='POST',extra=evidence))[0],401)
                    self.assertEqual((await call(http,'/api/admin/nominations',raw=raw,method='POST',extra=evidence,token='other'))[0],401)
                    self.assertEqual((await call(http,'/api/admin/nominations',raw=raw,method='POST',extra=evidence,claims_change={'iat':now,'exp':now+61}))[0],401)
                    wrong=dict(evidence);wrong['admin_nomination_v1']={**projection,'scope':{**projection['scope'],'deployment_id':'other'}}
                    self.assertEqual((await call(http,'/api/admin/nominations',raw=raw,method='POST',extra=wrong))[0],401)
                    try:
                        oversized=await call(http,'/api/admin/nominations',raw=raw,method='POST',extra=evidence,more_headers={'x-request-id':'x'*16384})
                    except ValueError:pass  # Sender refuses the same declared aggregate bound.
                    else:self.assertEqual(oversized[0],503)
                    self.assertEqual((await call(http,'/api/admin/nominations',raw=raw,method='POST',extra=evidence,claims_change={'padding':'x'*8192}))[0],401)
                    result=await call(http,'/api/admin/nominations',raw=raw,method='POST',extra=evidence)
                    self.assertEqual(result[0],200,result);self.assertEqual(result[1]['state'],'pending');self.assertIsNone(self.primary())
                    self.assertEqual((await call(http,'/api/admin/nominations',raw=raw,method='POST',extra=evidence))[0],409)
                    malformed=await call(http,'/api/me',account='owner-a',extra={'verified_contacts_v1':{'bad':'projection'}})
                    self.assertEqual(malformed[0],200,malformed);self.assertIsNone(self.primary())
                    contact=dict(scope=self.scope.model_dump(),account_id='owner-a',tags={'email':[TAG],'phone':[]})
                    before=self.row(cmd.nomination_id)
                    with patch('shadify_api.nomination_control.NominationControl._admin_audit',side_effect=RuntimeError('synthetic nomination audit failure')):
                        failed_claim=await call(http,'/api/me',account='owner-a',extra={'verified_contacts_v1':contact})
                    self.assertEqual(failed_claim[0],200,failed_claim)
                    self.assertFalse(failed_claim[1]['app_admin']['is_primary'])
                    self.assertIsNone(self.primary());self.assertEqual(self.row(cmd.nomination_id),before)
                    result=await call(http,'/api/me',account='owner-a',extra={'verified_contacts_v1':contact})
                    self.assertEqual(result[0],200,result);self.assertTrue(result[1]['app_admin']['is_primary']);self.assertEqual(self.primary(),'owner-a')
            finally:
                protocol.connection_lost(None)
                await binding.close();receiver.close()

