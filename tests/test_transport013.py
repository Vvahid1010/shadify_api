"""Exact official transport provenance and Shadify endpoint binding checks."""
import copy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
from uuid import uuid4

from shadify_api.security import ManagedReceiver

ROOT = Path(__file__).resolve().parents[1]

class Transport013Tests(unittest.TestCase):
    def test_loaded_official_source_matches_receipt(self):
        import node_agent_local_shell_transport as package
        dependency = json.loads((ROOT/'node_agent_transport_dependency.json').read_text())
        receipt = dependency['build_receipt']
        self.assertEqual(importlib.metadata.version('node-agent-local-shell-transport'), '0.1.13')
        directory = Path(package.__file__).parent
        embedded = json.loads((directory/'_source_provenance.json').read_text())
        self.assertEqual(embedded, {k:v for k,v in receipt.items() if k not in {'wheel','wheel_sha256'}})
        for name, digest in receipt['source_files'].items():
            if name.startswith('backend/node_agent_local_shell_transport/'):
                self.assertEqual(hashlib.sha256((directory/name.split('/')[-1]).read_bytes()).hexdigest(), digest)
        self.assertEqual(dependency['wheel_sha256'], '82b9964dbf0dca0736b49e22ef6dff834ba78a89e47f4bf805281929d1339cb5')

    def fixture(self):
        node, sender, receiver, cid = [str(uuid4()) for _ in range(4)]
        sender_id = dict(node_id=node, app_id='authentication', app_instance_id=sender)
        receiver_id = dict(node_id=node, app_id='shadify_api', app_instance_id=receiver)
        row = dict(node_id=node, sender_app_instance_id=sender, recipient_app_instance_id=receiver,
                   generation=1, record_sha256='a'*64, policy_revision=1, policy_digest='b'*64,
                   endpoint=dict(socket_path='/run/node-shadify/receiver.sock', sender_user='na-authentication',
                                 receiver_user='na-shadify', socket_group='shadify_transport',
                                 sender_app='authentication', receiver_app='shadify_api'))
        document = dict(profile='local_unix_v1', role='receiver', channels={cid:row})
        policy = dict(local=receiver_id, connections=[dict(connection_id=cid, policy_revision=1,
                      sender={'identity':sender_id}, recipient={'identity':receiver_id})])
        return document, policy

    def test_exact_endpoint_and_native_commitment_required(self):
        document, policy = self.fixture();binding=Mock();bounds=Mock()
        receiver=ManagedReceiver(binding,document,policy,bounds)
        binding.acquire.return_value.status.return_value=('shadify_api',receiver.channel.recipient_app_instance_id)
        binding.acquire.return_value.revisions.return_value=[(receiver.channel.connection_id,1,'b'*64)]
        self.assertTrue(receiver.current())
        binding.acquire.return_value.revisions.return_value=[(receiver.channel.connection_id,2,'b'*64)]
        self.assertFalse(receiver.current())
        receiver.close();self.assertFalse(receiver.current());bounds.close.assert_called_once()

    def test_invalid_descriptor_never_installs_admission(self):
        for fault in ('sender_app','receiver_app','root_user','unsafe_path','instance','connection','duplicate'):
            with self.subTest(fault=fault):
                document,policy=self.fixture();cid,row=next(iter(document['channels'].items()))
                if fault in ('sender_app','receiver_app'):row['endpoint'][fault]='incorrect_app'
                elif fault=='root_user':row['endpoint']['sender_user']='root'
                elif fault=='unsafe_path':row['endpoint']['socket_path']='/tmp/receiver.sock'
                elif fault=='instance':row['recipient_app_instance_id']=str(uuid4())
                elif fault=='connection':policy['connections'][0]['connection_id']=str(uuid4())
                else:document['channels'][str(uuid4())]=copy.deepcopy(row)
                binding=Mock()
                with self.assertRaises(ValueError):ManagedReceiver(binding,document,policy,Mock())
                binding.install_integration.assert_not_called()
