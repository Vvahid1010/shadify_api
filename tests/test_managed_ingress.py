"""Actual managed ingress + native ON shell + API; all peer/Redis inputs synthetic.

No socket is bound and no DB/R2/Redis server is contacted. Only SO_PEERCRED's
kernel return and Redis commands are fixtures; native crypto/admission is real.
"""
import asyncio
import base64
import hashlib
import json
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pqcrypto.kem import ml_kem_768
from pqcrypto.sign import ml_dsa_65
from app_security_shell._native import NativeContext
from app_security_shell.replay import RedisReplayStore
from app_security_shell.transport import TrustedPeer
from node_agent_local_shell_transport.local_uds import _HEADERS
from shadify_api.auth import require_account
from shadify_api.domain import DomainService
from shadify_api.main import create_app
from shadify_api.security import receiver_catalog
from shadify_api.runtime import create_runtime_app
from test_integration import identity_request


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",", ":")).encode()


def pem(label, value):
    value=base64.b64encode(value).decode()
    return "-----BEGIN "+label+"-----\n"+"\n".join(value[i:i+64] for i in range(0,len(value),64))+"\n-----END "+label+"-----\n"


def material(app):
    ed=Ed25519PrivateKey.generate();dp,ds=ml_dsa_65.keygen();kp,ks=ml_kem_768.keygen()
    instance=str(uuid4())
    return dict(schema_version=1,node_id=str(uuid4()),app_id=app,app_instance_id=instance,
                credential_owner_id=str(uuid4()),owner_app_instance_id=instance,generation=1,
                ed25519=dict(kid="ed-1",private_key_pem=ed.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode(),
                             public_key_pem=ed.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()),
                ml_dsa_65=dict(kid="dsa-1",private_key_pem=pem("ML-DSA-65 PRIVATE KEY",ds),public_key_pem=pem("ML-DSA-65 PUBLIC KEY",dp)),
                ml_kem_768=dict(kid="kem-1",private_key_pem=pem("ML-KEM-768 PRIVATE KEY",ks),public_key_pem=pem("ML-KEM-768 PUBLIC KEY",kp)))


def identity(value):
    return {k:value[k] for k in ("node_id","app_id","app_instance_id","generation")}


def peer(value):
    ed=serialization.load_pem_public_key(value["ed25519"]["public_key_pem"].encode())
    keys={"key_ids":{k:value[k]["kid"] for k in ("ed25519","ml_dsa_65","ml_kem_768")},
          "ed25519":base64.b64encode(ed.public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()}
    for k in ("ml_dsa_65","ml_kem_768"):
        keys[k]="".join(value[k]["public_key_pem"].splitlines()[1:-1])
    return dict(identity=identity(value),keys=keys)


class FixtureRedis:
    def __init__(self):self.values={}
    async def get(self,key):return self.values.get(key)
    async def set(self,key,value,nx=False):
        if not nx or key not in self.values:self.values[key]=value
    async def eval(self,script,count,marker,key,token,ttl):
        if self.values.get(marker)!=token:return -1
        if key in self.values:return 0
        self.values[key]="1";return 1


class CombinedIngressTests(unittest.TestCase):
    def test_real_shell_identity_tamper_peer_forgery_and_on_replay(self):
        asyncio.run(self.run_combined())

    def test_readiness_first_run_change_preserved_marker_lost_reservations(self):
        asyncio.run(self.run_combined(epoch_change="run_id"))

    def test_readiness_first_eviction_change_preserved_marker_lost_reservations(self):
        asyncio.run(self.run_combined(epoch_change="evicted_keys"))

    async def run_combined(self, epoch_change=None):
        with tempfile.TemporaryDirectory() as temporary,patch("node_agent_local_shell_transport.local_uds.pwd.getpwnam",return_value=Mock(pw_uid=1001)), \
             patch("socket.socket.connect",side_effect=AssertionError("network prohibited")), \
             patch("socket.socket.bind",side_effect=AssertionError("listener prohibited")), \
             patch("psycopg.connect",side_effect=AssertionError("database prohibited")), \
             patch("botocore.httpsession.URLLib3Session.send",side_effect=AssertionError("cloud prohibited")):
            root=Path(temporary);a=material("authentication");b=material("shadify_api");b["node_id"]=a["node_id"];connection=str(uuid4())
            digests={}
            for value in (a,b):
                directory=root/value["app_id"];directory.mkdir(mode=0o700)
                path=directory/"app_security.credentials.json";content=canonical(value);path.write_bytes(content);path.chmod(0o600)
                digests[value["app_id"]]=hashlib.sha256(content).hexdigest()
            common=dict(contract_version=1,extra_encryption_default=True,credentials_file="app_security.credentials.json",
                        connections=[dict(connection_id=connection,policy_revision=1,sender=peer(a),recipient=peer(b),
                                          endpoint_ids=[e["endpoint_id"] for e in receiver_catalog()])],endpoint_overrides=[])
            ap=common|dict(local=identity(a),credential_snapshot_sha256=digests[a["app_id"]])
            bp=common|dict(local=identity(b),credential_snapshot_sha256=digests[b["app_id"]])
            sender=NativeContext(str(root/a["app_id"]),canonical(ap),canonical(receiver_catalog()))
            projection=dict(app_security=bp,app_security_binding=dict(schema_version=1,destinations=[],outbound_endpoints=[]))
            ticks=[0.0];redis=FixtureRedis()
            async def close():pass
            def factory(instance,clock,storage):
                return RedisReplayStore(redis,"app-security:replay:"+instance,clock,storage_bounds=storage,monotonic=lambda:ticks[0]),close
            bounds=Mock();bounds.clock_ready.return_value=True;bounds.storage_ready.return_value=True
            if epoch_change is not None:
                from shadify_api.replay_bounds import LocalReplayBounds
                with patch("shadify_api.replay_bounds.Redis.from_url",return_value=Mock()):
                    bounds=LocalReplayBounds("redis://:synthetic@127.0.0.1")
                bounds.clock_ready=Mock(return_value=True)
                info=dict(run_id="a"*40,evicted_keys=0,role="master",connected_slaves=0,cluster_enabled=0,
                          loading=0,aof_enabled=0,maxmemory_policy="noeviction",maxmemory=1048576)
                bounds.client.info.return_value=info
            channel=dict(node_id=a["node_id"],sender_app_instance_id=a["app_instance_id"],
                         recipient_app_instance_id=b["app_instance_id"],generation=1,record_sha256="a"*64,
                         policy_revision=1,policy_digest=sender.transport_binding(connection)[5])
            repo=Mock();repo.project_user.return_value=dict(user_id=str(uuid4()),account_id="owner",artist_ids=[])
            document=dict(profile="local_unix_v1",role="receiver",channels={connection:channel})
            directory=root/b["app_id"]
            for name,value in {"shadify_api.config.json":projection|dict(app_security_transport=document),
                               "app_profile.json":{"schema_version":1,"app":"shadify_api"},
                               "replay.credentials.json":{"url":"redis://:synthetic@127.0.0.1:6379/0"},
                               "database.credentials.json":{"host":"127.0.0.1","dbname":"synthetic","user":"synthetic","password":"synthetic","sslmode":"disable"}}.items():
                path=directory/name;path.write_bytes(canonical(value));path.chmod(0o600)
            with patch("shadify_api.replay_bounds.LocalReplayBounds",return_value=bounds), \
                 patch("shadify_api.runtime.replay_factory",return_value=factory), \
                 patch("shadify_api.runtime.PostgresMediaRepository",return_value=repo):
                app=create_runtime_app(directory)
            binding=app.state.app_security;receiver=app.state.managed_receiver
            config=uvicorn.Config(app,log_config=None);config.load()
            protocol=receiver.http_protocol(config=config,server_state=__import__("uvicorn.server",fromlist=["ServerState"]).ServerState(),app_state={})
            sock=Mock();sock.family=socket.AF_UNIX;sock.getsockopt.return_value=struct.pack("3i",1888,1001,1001)
            transport=Mock();transport.get_extra_info.side_effect=lambda name,default=None: sock if name=="socket" else default
            protocol.connection_made(transport)
            ingress_headers={name.decode():value.decode() for name,value in zip(_HEADERS,receiver.channel.metadata,strict=True)}
            body=dict(method="GET",path="/api/me",query_b64="",headers=[dict(name=k.decode(),value_b64=base64.b64encode(v).decode())
                     for k,v in identity_request().scope["headers"]],body_b64="",status=None,response_to=None)
            def ticket():return sender.begin(connection,"shadify_api.me.read",canonical(body),int(time.time()))
            async def call(http,wire,headers=None):
                return await http.post("http://fixture/_app-security/v1/message",content=wire,
                                       headers={**ingress_headers,"content-type":"application/app-security+json",**(headers or {})})
            try:
                await binding.ready()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(protocol.app)) as http:
                    first=ticket()
                    self.assertEqual((await call(http,first.wire())).status_code,503) # Canonical recovery gate.
                    pending=await http.get("http://fixture/health/ready")
                    self.assertEqual(pending.status_code,503)
                    self.assertEqual(pending.json()["checks"]["replay_admission"],"recovering_or_unavailable")
                    ticks[0]=152
                    response=await call(http,first.wire())
                    self.assertEqual(response.status_code,200,response.text)
                    decoded=json.loads(first.complete(response.content,int(time.time())))
                    self.assertEqual(decoded["status"],200)
                    self.assertEqual(json.loads(base64.b64decode(decoded["body_b64"]))["account_id"],"owner")
                    repo.project_user.assert_called_once_with("owner")
                    checked=await http.get("http://fixture/health/ready")
                    self.assertEqual(checked.status_code,503) # R2 deliberately absent.
                    self.assertEqual(checked.json()["checks"]["replay_admission"],"ready")
                    self.assertEqual(checked.json()["checks"]["storage"],"missing")
                    if epoch_change is not None:
                        # Lose reservations but keep the real canonical marker token.
                        markers={key:value for key,value in redis.values.items() if key.endswith(":continuity")}
                        self.assertEqual(len(markers),1)
                        self.assertGreater(len(redis.values),len(markers))
                        redis.values.clear();redis.values.update(markers)
                        info[epoch_change]="b"*40 if epoch_change=="run_id" else 1
                        observed=await http.get("http://fixture/health/ready") # First consumer after epoch change.
                        self.assertEqual(observed.status_code,503)
                        self.assertEqual(observed.json()["checks"]["replay_storage_bounds"],"unavailable")
                        self.assertEqual(redis.values,markers) # Never reset/change the continuity marker.
                        self.assertEqual((await call(http,first.wire())).status_code,503)
                        self.assertEqual(observed.json()["checks"]["replay_admission"],"recovering_or_unavailable")
                        self.assertEqual(repo.project_user.call_count,1)
                        ticks[0]=302 # Only150seconds since renewed recovery started at152.
                        self.assertEqual((await call(http,first.wire())).status_code,503)
                        self.assertEqual(repo.project_user.call_count,1)
                        self.assertEqual(redis.values,markers)
                        ticks[0]=303
                        recovered=ticket()
                        result=await call(http,recovered.wire())
                        self.assertEqual(result.status_code,200,result.text)
                        self.assertEqual(repo.project_user.call_count,2)
                        for key,value in markers.items():self.assertEqual(redis.values[key],value)
                        return
                    self.assertEqual((await call(http,first.wire())).status_code,503)
                    fresh=ticket()
                    self.assertEqual((await call(http,fresh.wire(),{"x-node-agent-local-generation":"2"})).status_code,403)
                    wire=json.loads(fresh.wire());wire["enc"]["ciphertext"]="AA=="
                    self.assertEqual((await call(http,canonical(wire))).status_code,503)
                    forged=dict(identity_request().scope["headers"])
                    response=await http.get("http://fixture/api/me",headers={**ingress_headers,**{k.decode():v.decode() for k,v in forged.items()}})
                    self.assertEqual(response.status_code,503)
                    # A caller-supplied admitted marker is removed by GuardedASGI.
                    async def forged_scope(scope,receive,send):
                        scope=dict(scope);scope["app_security.admitted"]=TrustedPeer(connection,a["node_id"],a["app_instance_id"])
                        await protocol.app(scope,receive,send)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(forged_scope)) as direct:
                        self.assertEqual((await direct.get("http://fixture/api/me",headers=ingress_headers)).status_code,503)
                    self.assertEqual(repo.project_user.call_count,1)
                    receiver.active=False
                    self.assertEqual((await call(http,ticket().wire())).status_code,403)
                    receiver.active=True
                    wrong_sock=Mock();wrong_sock.family=socket.AF_UNIX
                    wrong_sock.getsockopt.return_value=struct.pack("3i",1888,1002,1002)
                    wrong_transport=Mock();wrong_transport.get_extra_info.side_effect=lambda name,default=None: wrong_sock if name=="socket" else default
                    wrong=receiver.http_protocol(config=config,server_state=__import__("uvicorn.server",fromlist=["ServerState"]).ServerState(),app_state={})
                    wrong.connection_made(wrong_transport)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrong.app)) as wrong_http:
                        self.assertEqual((await call(wrong_http,ticket().wire())).status_code,403)
                    wrong.connection_lost(None)
                    bounds.clock_ready.return_value=False
                    self.assertEqual((await call(http,ticket().wire())).status_code,503)
                    self.assertEqual(receiver.readiness()["clock_bounds"],"unavailable")
            finally:
                protocol.connection_lost(None)
                await binding.close();receiver.close()


if __name__=="__main__":unittest.main()
