"""Descriptor/path checks use real files; root ACL and mount metadata are fixtures.

The metadata matches the independently observed Node B systemd projection. No
credential access, mount, service, native publication or live runtime is changed.
"""
import json
import os
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from shadify_api import config


def acl(uid, permissions, group=0):
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *row) for row in (
        (1, permissions, 0xffffffff), (2, permissions, uid),
        (4, group, 0xffffffff), (16, permissions, 0xffffffff), (32, 0, 0xffffffff)))


class SystemdProtectedJson(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "fixture.service"
        self.directory.mkdir()
        self.path = self.directory / "shadify_api.config.json"
        self.path.write_text(json.dumps({"synthetic": "input"}))
        self.path.chmod(0o440)
        self.directory.chmod(0o550)
        self.real_fstat = os.fstat
        self.flags = os.ST_RDONLY | os.ST_NOSUID
        self.file_mode, self.directory_mode = 0o440, 0o550
        self.file_uid, self.file_gid = 0, 0
        self.directory_uid, self.directory_gid = 0, 0
        self.service_uid = os.geteuid()
        self.group_access = 0
        self.extra_acl = b""
        self.env = patch.dict(os.environ, CREDENTIALS_DIRECTORY=str(self.directory))
        self.root_patch = patch.object(config, "SYSTEMD_CREDENTIALS_ROOT", self.root)
        self.env.start(); self.root_patch.start()
        self.addCleanup(self.env.stop); self.addCleanup(self.root_patch.stop)

    def tearDown(self):
        self.directory.chmod(0o700)
        self.temp.cleanup()

    def metadata(self, fd):
        values=list(self.real_fstat(fd))
        directory=stat.S_ISDIR(values[0])
        values[0]=(stat.S_IFDIR if directory else stat.S_IFREG) | (self.directory_mode if directory else self.file_mode)
        values[4]=self.directory_uid if directory else self.file_uid
        values[5]=self.directory_gid if directory else self.file_gid
        return os.stat_result(values)

    def xattr(self, fd, name):
        directory=stat.S_ISDIR(self.real_fstat(fd).st_mode)
        return acl(self.service_uid, 5 if directory else 4, self.group_access)+self.extra_acl

    def projected(self, operation=None):
        with patch.object(config.os, "fstat", side_effect=self.metadata), \
             patch.object(config.os, "fstatvfs", return_value=SimpleNamespace(f_flag=self.flags)), \
             patch.object(config.os, "getxattr", side_effect=self.xattr):
            return (operation or (lambda: config.load_protected_json(self.path)))()

    def reject(self):
        with self.assertRaises(ValueError): self.projected()

    def test_observed_root_service_only_readonly_projection(self):
        self.assertEqual(self.projected(), {"synthetic":"input"})

    def test_ordinary_group_readable_file_stays_rejected(self):
        with self.assertRaises(ValueError): config.load_protected_json(self.path)

    def test_missing_directory_binding_denies(self):
        with patch.dict(os.environ, CREDENTIALS_DIRECTORY=""): self.reject()

    def test_other_directory_denies(self):
        with patch.dict(os.environ, CREDENTIALS_DIRECTORY=str(self.root)): self.reject()

    def test_outside_systemd_root_denies(self):
        with patch.object(config, "SYSTEMD_CREDENTIALS_ROOT", Path("/run/credentials")): self.reject()

    def test_writable_mount_denies(self):
        self.flags=os.ST_NOSUID; self.reject()

    def test_mount_without_nosuid_denies(self):
        self.flags=os.ST_RDONLY; self.reject()

    def test_group_access_acl_denies(self):
        self.group_access=4; self.reject()

    def test_other_service_acl_denies(self):
        self.service_uid+=100; self.reject()

    def test_extra_acl_principal_denies(self):
        self.extra_acl=struct.pack("<HHI", 8, 4, 1234); self.reject()

    def test_missing_acl_denies(self):
        self.xattr=lambda *args: (_ for _ in ()).throw(OSError("fixture")); self.reject()

    def test_foreign_file_owner_denies(self):
        self.file_uid=os.geteuid()+100; self.reject()

    def test_nonroot_file_group_denies(self):
        self.file_gid=100; self.reject()

    def test_nonroot_directory_denies(self):
        self.directory_uid=100; self.reject()

    def test_writable_directory_denies(self):
        self.directory_mode=0o750; self.reject()

    def test_world_readable_file_denies(self):
        self.file_mode=0o444; self.reject()

    def test_symlink_denies(self):
        self.directory.chmod(0o700)
        link=self.directory/'link.json'; link.symlink_to(self.path)
        with self.assertRaises(OSError): config.load_protected_json(link)

    def test_ordinary_owner_only_unchanged(self):
        self.path.chmod(0o600)
        self.assertEqual(config.load_protected_json(self.path), {"synthetic":"input"})

    def test_real_runtime_factory_accepts_projected_config_and_profile(self):
        self.directory.chmod(0o700)
        self.path.chmod(0o600)
        self.path.write_text(json.dumps({"app_security":{"local":{"app_id":"shadify_api"}},"app_security_binding":{}}))
        self.path.chmod(0o440)
        profile=self.directory/'app_profile.json'
        profile.write_text(json.dumps({"schema_version":1,"app":"shadify_api","components":{}})); profile.chmod(0o440)
        self.directory.chmod(0o550)
        import importlib
        with patch("psycopg.connect", side_effect=AssertionError("no database effects")):
            app=self.projected(lambda: importlib.import_module("shadify_api.runtime").create_runtime_app(self.directory))
        self.assertIsNotNone(app)


    def test_full_five_file_83_route_factory_with_projected_json_and_native_owner_only_fixture(self):
        import asyncio
        import hashlib
        from uuid import uuid4
        from unittest.mock import Mock
        # Import the existing synthetic native fixture with no selected runtime.
        with patch.dict(os.environ, CREDENTIALS_DIRECTORY=""):
            from test_managed_ingress import material, peer, identity, canonical, FixtureRedis
            from app_security_shell._native import NativeContext
            from app_security_shell.replay import RedisReplayStore
            from shadify_api.security import receiver_catalog
            from shadify_api.runtime import create_runtime_app
        self.assertEqual(len(receiver_catalog()), 83)
        self.directory.chmod(0o700)
        a=material("authentication"); b=material("shadify_api"); b["node_id"]=a["node_id"]
        connection=str(uuid4()); sender_directory=self.root/'sender'
        sender_directory.mkdir(mode=0o700)
        digests={}
        for value, directory in ((a,sender_directory),(b,self.directory)):
            path=directory/'app_security.credentials.json'
            path.write_bytes(canonical(value)); path.chmod(0o600)
            digests[value['app_id']]=hashlib.sha256(path.read_bytes()).hexdigest()
        common=dict(contract_version=1,extra_encryption_default=True,credentials_file="app_security.credentials.json",
                    connections=[dict(connection_id=connection,policy_revision=1,sender=peer(a),recipient=peer(b),
                                      endpoint_ids=[e['endpoint_id'] for e in receiver_catalog()])],endpoint_overrides=[])
        ap=common|dict(local=identity(a),credential_snapshot_sha256=digests['authentication'])
        bp=common|dict(local=identity(b),credential_snapshot_sha256=digests['shadify_api'])
        sender=NativeContext(str(sender_directory),canonical(ap),canonical(receiver_catalog()))
        channel=dict(node_id=a['node_id'],sender_app_instance_id=a['app_instance_id'],
                     recipient_app_instance_id=b['app_instance_id'],generation=1,record_sha256='a'*64,
                     policy_revision=1,policy_digest=sender.transport_binding(connection)[5])
        values={
            'shadify_api.config.json':dict(app_security=bp,app_security_binding=dict(schema_version=1,destinations=[],outbound_endpoints=[]),
                app_security_transport=dict(profile='local_unix_v1',role='receiver',channels={connection:channel})),
            'app_profile.json':dict(schema_version=1,app='shadify_api'),
            'replay.credentials.json':dict(url='redis://:synthetic@127.0.0.1:6379/0'),
            'database.credentials.json':dict(host='127.0.0.1',dbname='synthetic',user='synthetic',password='synthetic',sslmode='disable')}
        for name,value in values.items():
            path=self.directory/name; path.chmod(0o600) if path.exists() else None
            path.write_bytes(canonical(value)); path.chmod(0o440)
        # Native Rust independently validates actual kernel ACL/mount metadata.
        # Retain its normal owner-only fixture; only Python JSON projection
        # metadata is mocked. This is not a real systemd native-projection test.
        (self.directory/'app_security.credentials.json').chmod(0o600)
        self.directory.chmod(0o700)
        redis=FixtureRedis()
        async def close(): pass
        def replay(instance,clock,storage):
            return RedisReplayStore(redis,'app-security:replay:'+instance,clock,storage_bounds=storage),close
        bounds=Mock(); bounds.clock_ready.return_value=True; bounds.storage_ready.return_value=True
        async def verify():
            app=self.projected(lambda: create_runtime_app(self.directory)); binding=app.state.app_security
            restored=None
            try:
                await binding.ready(); checkpoint=binding.checkpoint()
                self.assertEqual(binding.acquire().status()[0],'shadify_api')
                restored=self.projected(lambda: create_runtime_app(self.directory)).state.app_security
                candidate=await binding.jobs.run(restored.prepare)
                restored.restore(candidate,checkpoint=checkpoint,overlaps={})
                self.assertEqual(restored.checkpoint(),checkpoint)
                self.assertEqual(restored.acquire().status()[0],'shadify_api')
            finally:
                if restored is not None:await restored.close()
                await binding.close()
        with patch('shadify_api.replay_bounds.LocalReplayBounds',return_value=bounds), \
             patch('shadify_api.runtime.replay_factory',return_value=replay), \
             patch('psycopg.connect',side_effect=AssertionError('database prohibited')), \
             patch('socket.socket.connect',side_effect=AssertionError('network prohibited')), \
             patch('socket.socket.bind',side_effect=AssertionError('listener prohibited')):
            asyncio.run(verify())


if __name__ == "__main__": unittest.main()
