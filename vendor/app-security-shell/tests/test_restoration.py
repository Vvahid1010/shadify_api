"""Fresh native reconstruction from public, authority-bound test checkpoints."""
import copy
import json
import os
import subprocess
import sys

import pytest

from app_security_shell._native import NativeContext
from app_security_shell.lifecycle import ContextRegistry
from test_pair import pair as pair
from test_pair import canonical, identity, payload
from test_rotation_overlap import rotated


def prepared(pair, tmp_path):
    a, b = pair[0](registries=True)
    old = b.acquire()
    _, inputs = rotated(pair, tmp_path)
    b.install(b.prepare(*inputs), overlap=('pair',))
    return a, b, old, inputs


def test_restore_old_admission_and_reset_retirement_clock(pair, tmp_path):
    a, b, old, inputs = prepared(pair, tmp_path)
    b.mark_cutover('pair', **b.overlap_status('pair'))
    checkpoint = b.checkpoint()
    fresh = ContextRegistry(clock=lambda: 10**9)
    fresh.restore(NativeContext(*inputs), checkpoint=checkpoint, overlaps={'pair': old})
    wire = a.acquire().begin('pair', 'echo', canonical(payload()), 100).wire()
    assert fresh.acquire_inbound(wire) is old
    with pytest.raises(ValueError, match='retirement'):
        fresh.retire_overlap('pair', **fresh.overlap_status('pair'))
    assert fresh.checkpoint() == checkpoint
    with pytest.raises(ValueError, match='fresh_registry'):
        fresh.restore(NativeContext(*inputs), checkpoint=checkpoint, overlaps={'pair': old})


@pytest.mark.parametrize('fault', ['stale_active', 'revoked_old', 'missing_old', 'wrong_old', 'missing_floor', 'duplicate_floor', 'missing_routes', 'bool_revision'])
def test_invalid_restore_is_unusable_and_cannot_fallback(pair, tmp_path, fault):
    _, b, old, inputs = prepared(pair, tmp_path)
    checkpoint = b.checkpoint()
    candidate = NativeContext(*inputs)
    overlaps = {'pair': old}
    if fault == 'stale_active': candidate = old
    if fault == 'revoked_old': checkpoint['revoked'] = [[pair[1]['app_instance_id'], 1]]
    if fault == 'missing_old': overlaps = {}
    if fault == 'wrong_old': overlaps = {'pair': candidate}
    if fault == 'missing_floor': checkpoint['generations'] = []
    if fault == 'duplicate_floor': checkpoint['revisions'] *= 2
    if fault == 'missing_routes': checkpoint['receiver_routes'] = []
    if fault == 'bool_revision': checkpoint['active']['revisions'][0][1] = True
    fresh = ContextRegistry()
    with pytest.raises(ValueError):
        fresh.restore(candidate, checkpoint=checkpoint, overlaps=overlaps)
    with pytest.raises(ValueError): fresh.acquire()
    assert ('POST', '/echo') in fresh.receiver_routes()
    with pytest.raises(ValueError): fresh.prepare(*inputs)


def test_removed_connection_and_blocked_restore_preserve_denial(pair, tmp_path):
    _, b, old, inputs = prepared(pair, tmp_path)
    config = json.loads(inputs[1]); config['connections'] = []
    empty_inputs = (inputs[0], canonical(config), inputs[2])
    b.install(b.prepare(*empty_inputs))
    fresh = ContextRegistry()
    fresh.restore(NativeContext(*empty_inputs), checkpoint=b.checkpoint(), overlaps={})
    assert ('POST', '/echo') in fresh.receiver_routes()
    with pytest.raises(ValueError): fresh.restore(old, checkpoint=b.checkpoint(), overlaps={})
    # A fresh process must not re-enroll an old removed revision.
    with pytest.raises(ValueError): fresh.prepare(*inputs)
    _, b = pair[0](registries=True)
    b.revoke(pair[2]['app_instance_id'], 1)
    blocked = ContextRegistry()
    blocked.restore(None, checkpoint=b.checkpoint(), overlaps={})
    assert ('POST', '/echo') in blocked.receiver_routes()
    with pytest.raises(ValueError): blocked.acquire()


def test_restore_in_fresh_python_process(pair, tmp_path):
    _, b, _, inputs = prepared(pair, tmp_path)
    _, directory, _, digest, common, catalog = pair[0].inputs
    old_inputs = [str(directory), common | {'local': identity(pair[2]), 'credential_snapshot_sha256': digest}, catalog]
    fixture = tmp_path / 'public-restore.json'
    fixture.write_text(json.dumps({'checkpoint': b.checkpoint(), 'current': [inputs[0], json.loads(inputs[1]), json.loads(inputs[2])], 'old': old_inputs}))
    script = '''
import json,sys
from app_security_shell._native import NativeContext
from app_security_shell.lifecycle import ContextRegistry
v=json.load(open(sys.argv[1]))
def native(row):
 return NativeContext(row[0],json.dumps(row[1]).encode(),json.dumps(row[2]).encode())
r=ContextRegistry()
r.restore(native(v['current']),checkpoint=v['checkpoint'],overlaps={'pair':native(v['old'])})
assert r.checkpoint()==v['checkpoint']
try:r.retire_overlap('pair',**r.overlap_status('pair'))
except ValueError:pass
else:raise AssertionError('retirement was restored without proof')
print('fresh restore verified')
'''
    result = subprocess.run([sys.executable, '-c', script, str(fixture)], env=os.environ.copy(), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'fresh restore verified'


@pytest.mark.parametrize('tamper', [False, True])
def test_binding_restore_binds_actual_destination(pair, tamper):
    import asyncio
    from app_security_shell.binding import AppSecurityBinding
    directory, _, digest, _, common, catalog = pair[0].inputs
    projection = {
        'app_security': common | {'local': identity(pair[1]), 'credential_snapshot_sha256': digest},
        'app_security_binding': {'schema_version': 1, 'destinations': [{'connection_id': 'pair', 'base_url': 'http://fixture'}], 'outbound_endpoints': catalog},
    }
    def binding(value):
        return AppSecurityBinding(snapshot_provider=lambda: value, directory_provider=lambda: directory, receiver_catalog=[])
    original = binding(projection)
    original.install(original.prepare())
    checkpoint = original.checkpoint()
    selected = copy.deepcopy(projection)
    if tamper:
        selected['app_security_binding']['destinations'][0]['base_url'] = 'http://changed'
    fresh = binding(selected)
    try:
        candidate = fresh.prepare()
        if tamper:
            with pytest.raises(ValueError, match='projection'):
                fresh.restore(candidate, checkpoint=checkpoint, overlaps={})
            with pytest.raises(ValueError): fresh.acquire()
            with pytest.raises(ValueError): fresh.install(candidate)
        else:
            fresh.restore(candidate, checkpoint=checkpoint, overlaps={})
            assert fresh.checkpoint() == checkpoint
    finally:
        asyncio.run(original.close())
        asyncio.run(fresh.close())


def test_empty_native_publication_still_binds_node_identity(pair):
    _, directory, _, digest, common, catalog = pair[0].inputs
    config = copy.deepcopy(common) | {'local': identity(pair[2]), 'credential_snapshot_sha256': digest}
    config['connections'] = []
    registry = ContextRegistry()
    registry.install(registry.prepare(str(directory), canonical(config), canonical(catalog)))
    checkpoint = registry.checkpoint()
    config['local']['node_id'] = '66666666-6666-4666-8666-666666666666'
    candidate = NativeContext(str(directory), canonical(config), canonical(catalog))
    with pytest.raises(ValueError, match='publication'):
        ContextRegistry().restore(candidate, checkpoint=checkpoint, overlaps={})


def test_blocked_binding_restore_keeps_historical_route_guard(pair):
    import asyncio
    from app_security_shell.binding import AppSecurityBinding
    from app_security_shell.receiver import GuardedASGI
    _, directory, _, digest, common, catalog = pair[0].inputs
    projection = {
        'app_security': common | {'local': identity(pair[2]), 'credential_snapshot_sha256': digest},
        'app_security_binding': {'schema_version': 1, 'destinations': [], 'outbound_endpoints': []},
    }
    original = AppSecurityBinding(snapshot_provider=lambda: projection, directory_provider=lambda: directory, receiver_catalog=catalog)
    original.install(original.prepare())
    original.revoke(pair[2]['app_instance_id'], 1)
    checkpoint = original.checkpoint()
    # Current app catalog may no longer declare the removed historical route.
    fresh = AppSecurityBinding(snapshot_provider=lambda: projection, directory_provider=lambda: directory, receiver_catalog=[])
    try:
        fresh.restore(None, checkpoint=checkpoint, overlaps={})
        assert fresh.checkpoint() == checkpoint
        with pytest.raises(ValueError): fresh.acquire()
        assert ('POST', '/echo') in fresh.receiver_routes()
        called, sent = [], []
        async def business(*args): called.append(True)
        async def receive(): return {'type': 'http.request', 'body': b''}
        async def send(value): sent.append(value)
        asyncio.run(GuardedASGI(business, binding=fresh)({'type': 'http', 'method': 'POST', 'path': '/echo'}, receive, send))
        assert not called and sent[0]['status'] == 503
    finally:
        asyncio.run(original.close()); asyncio.run(fresh.close())


@pytest.mark.parametrize('prepared_first', [False, True])
def test_pre_restore_revocation_is_never_erased(pair, tmp_path, prepared_first):
    _, b, old, inputs = prepared(pair, tmp_path)
    checkpoint = b.checkpoint()
    fresh = ContextRegistry()
    candidate = fresh.prepare(*inputs) if prepared_first else NativeContext(*inputs)
    fresh.revoke(pair[2]['app_instance_id'], 1)
    with pytest.raises(ValueError):
        fresh.restore(candidate, checkpoint=checkpoint, overlaps={'pair': old})
    with pytest.raises(ValueError): fresh.acquire()


def test_binding_revoke_between_prepare_and_restore_wins(pair):
    import asyncio
    from app_security_shell.binding import AppSecurityBinding
    directory, _, digest, _, common, catalog = pair[0].inputs
    projection = {
        'app_security': common | {'local': identity(pair[1]), 'credential_snapshot_sha256': digest},
        'app_security_binding': {'schema_version': 1, 'destinations': [{'connection_id': 'pair', 'base_url': 'http://fixture'}], 'outbound_endpoints': catalog},
    }
    def binding():
        return AppSecurityBinding(snapshot_provider=lambda: projection, directory_provider=lambda: directory, receiver_catalog=[])
    original, fresh = binding(), binding()
    try:
        original.install(original.prepare())
        checkpoint = original.checkpoint()
        candidate = fresh.prepare()
        fresh.revoke(pair[1]['app_instance_id'], 1)
        with pytest.raises(ValueError):
            fresh.restore(candidate, checkpoint=checkpoint, overlaps={})
        with pytest.raises(ValueError): fresh.acquire()
    finally:
        asyncio.run(original.close()); asyncio.run(fresh.close())
