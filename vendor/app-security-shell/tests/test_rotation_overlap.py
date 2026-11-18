"""Real native contexts; isolated generated keys, no managed runtime."""
import copy
import json

import pytest

from app_security_shell._native import NativeContext
from app_security_shell.lifecycle import ContextRegistry
from test_pair import pair, canonical, identity, peer, payload
from test_key_admission import material as material_fixture, write_material


def rotated(pair, tmp_path, *, revision=2, mode=True):
    directory, receiver, _, digest, common, catalog = pair[0].inputs
    material = material_fixture.__wrapped__()
    material.update(identity(pair[1]) | {'generation': revision})
    sender_dir = tmp_path / f'rotation-{revision}'
    _, sender_digest = write_material(sender_dir, material)
    config = copy.deepcopy(common)
    config['connections'][0].update(sender=peer(material), policy_revision=revision)
    if not mode:
        config['endpoint_overrides'] = [{'connection_id': 'pair', 'endpoint_id': 'echo', 'extra_encryption': False}]
    a = NativeContext(str(sender_dir), canonical(config | {'local': identity(material), 'credential_snapshot_sha256': sender_digest}), canonical(catalog))
    b_config = canonical(config | {'local': identity(pair[2]), 'credential_snapshot_sha256': digest})
    return a, (str(receiver), b_config, canonical(catalog))


def test_explicit_overlap_routes_one_exact_snapshot_and_full_verification(pair, tmp_path):
    a, b = pair[0](registries=True)
    old = b.acquire()
    pending = a.acquire().begin('pair', 'echo', canonical(payload()), 100)
    new_a, inputs = rotated(pair, tmp_path)
    b.install(b.prepare(*inputs), overlap=('pair',))
    assert b.acquire() is not old
    assert b.acquire_inbound(pending.wire()) is old
    new = new_a.begin('pair', 'echo', canonical(payload()), 100)
    assert b.acquire_inbound(new.wire()) is b.acquire()
    admitted = old.receive(pending.wire(), 100)
    assert '204' in pending.complete(admitted.respond(canonical(payload(b'', 204)), 101), 101)
    forged = json.loads(pending.wire())
    forged['meta']['policy_revision'] = 999
    with pytest.raises(ValueError):
        b.acquire_inbound(canonical(forged))
    forged = json.loads(pending.wire())
    forged['signatures']['ed25519'] = 'A' * 88
    selected = b.acquire_inbound(canonical(forged))
    with pytest.raises(ValueError):
        selected.receive(canonical(forged), 100)


def test_rotation_requires_explicit_admission_and_serial_retirement(pair, tmp_path):
    a, b = pair[0](registries=True)
    wire = a.acquire().begin('pair', 'echo', canonical(payload()), 100).wire()
    _, inputs = rotated(pair, tmp_path)
    b.install(b.prepare(*inputs))
    with pytest.raises(ValueError):
        b.acquire_inbound(wire)
    a, b = pair[0](registries=True)
    b.install(b.prepare(*inputs), overlap=('pair',))
    _, next_inputs = rotated(pair, tmp_path, revision=3)
    with pytest.raises(ValueError, match='overlap_not_retired'):
        b.install(b.prepare(*next_inputs), overlap=('pair',))
    with pytest.raises(ValueError, match='retirement'):
        b.retire_overlap('pair', **b.overlap_status('pair'))


def test_old_revocation_and_mode_change_deny_new_admission(pair, tmp_path):
    a, b = pair[0](registries=True)
    wire = a.acquire().begin('pair', 'echo', canonical(payload()), 100).wire()
    old = b.acquire()
    _, inputs = rotated(pair, tmp_path)
    b.install(b.prepare(*inputs), overlap=('pair',))
    b.revoke(pair[1]['app_instance_id'], 1)
    with pytest.raises(ValueError):
        b.acquire_inbound(wire)
    with pytest.raises(ValueError):
        b.admit_inbound(old, 'pair')
    a, b = pair[0](registries=True)
    _, changed = rotated(pair, tmp_path, revision=4, mode=False)
    with pytest.raises(ValueError):
        b.install(b.prepare(*changed), overlap=('pair',))


def test_retirement_floor_and_no_automatic_expiry(pair, tmp_path):
    a, b = pair[0](registries=True)
    now = [1000.0]
    b._clock = lambda: now[0]
    wire = a.acquire().begin('pair', 'echo', canonical(payload()), 100).wire()
    _, inputs = rotated(pair, tmp_path)
    b.install(b.prepare(*inputs), overlap=('pair',))
    now[0] += 500
    with pytest.raises(ValueError):
        b.retire_overlap('pair', **b.overlap_status('pair'))
    b.mark_cutover('pair', **b.overlap_status('pair'))
    now[0] += 200
    b.mark_cutover('pair', **b.overlap_status('pair'))
    now[0] += 210.999
    with pytest.raises(ValueError):
        b.retire_overlap('pair', **b.overlap_status('pair'))
    now[0] += .001
    b.acquire_inbound(wire)
    b.retire_overlap('pair', **b.overlap_status('pair'))
    with pytest.raises(ValueError):
        b.acquire_inbound(wire)


@pytest.mark.parametrize('revoke_during_replay', [False, True])
def test_old_inbound_dispatch_obeys_current_fence(pair, tmp_path, revoke_during_replay):
    import asyncio
    from app_security_shell.transport import Middleware
    from test_pair import FixtureReplay, fixture_ingress

    async def run():
        a, b = pair[0](registries=True)
        pending = a.acquire().begin('pair', 'echo', canonical(payload()), 100)
        _, inputs = rotated(pair, tmp_path)
        b.install(b.prepare(*inputs), overlap=('pair',))
        seen, sent = [], []

        class Replay(FixtureReplay):
            async def reserve(self, *args):
                await super().reserve(*args)
                if revoke_during_replay:
                    b.revoke(pair[1]['app_instance_id'], 1)

        async def business(scope, receive, send):
            seen.append(scope['path'])
            await send({'type': 'http.response.start', 'status': 204, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        async def receive():
            return {'type': 'http.request', 'body': pending.wire()}

        async def send(message):
            sent.append(message)

        wrapper = Middleware(business, b, Replay(), clock=lambda: 100,
                             trusted_ingress=fixture_ingress(pair[1]))
        try:
            await wrapper({'type': 'http', 'method': 'POST', 'path': '/_app-security/v1/message',
                           'headers': [(b'content-type', b'application/app-security+json')]}, receive, send)
            assert seen == ([] if revoke_during_replay else ['/echo'])
            assert sent[0]['status'] == (503 if revoke_during_replay else 200)
            if not revoke_during_replay:
                assert '204' in pending.complete(sent[1]['body'], 100)
        finally:
            await wrapper.jobs.close()

    asyncio.run(run())


def test_off_overlap_preserves_mode_and_exact_policy(pair, tmp_path):
    a, b = pair[0](off=True, registries=True)
    old = b.acquire()
    metadata = a.acquire().plain_policy('pair', 'echo')
    _, inputs = rotated(pair, tmp_path, mode=False)
    b.install(b.prepare(*inputs), overlap=('pair',))
    assert b.acquire_plain('pair', metadata) is old
    with pytest.raises(ValueError):
        b.acquire_plain('pair', metadata.replace('echo', 'unknown'))
    # An ON switch removes the old OFF admission immediately.
    _, on_inputs = rotated(pair, tmp_path, revision=3)
    b.install(b.prepare(*on_inputs))
    with pytest.raises(ValueError):
        b.acquire_plain('pair', metadata)
    with pytest.raises(ValueError):
        b.admit_inbound(old, 'pair')


def test_cutover_proof_cannot_target_another_rotation(pair, tmp_path):
    _, b = pair[0](registries=True)
    now = [1000.0]
    b._clock = lambda: now[0]
    _, inputs = rotated(pair, tmp_path)
    b.install(b.prepare(*inputs), overlap=('pair',))
    previous = b.overlap_status('pair')
    b.mark_cutover('pair', **previous)
    now[0] += 211
    b.retire_overlap('pair', **previous)
    _, inputs = rotated(pair, tmp_path, revision=3)
    b.install(b.prepare(*inputs), overlap=('pair',))
    with pytest.raises(ValueError, match='stale_overlap'):
        b.mark_cutover('pair', **previous)
    with pytest.raises(ValueError, match='stale_overlap'):
        b.retire_overlap('pair', **previous)


def test_removed_connection_drops_old_and_limits_are_atomic(pair, tmp_path):
    a, b = pair[0](registries=True)
    wire = a.acquire().begin('pair', 'echo', canonical(payload()), 100).wire()
    _, inputs = rotated(pair, tmp_path)
    candidate = b.prepare(*inputs)
    active = b.acquire()
    with pytest.raises(ValueError, match='overlap_limit'):
        b.install(candidate, overlap=['pair'] * 65)
    assert b.acquire() is active
    b.install(candidate, overlap=('pair',))
    config = json.loads(inputs[1])
    config['connections'] = []
    b.install(b.prepare(inputs[0], canonical(config), inputs[2]))
    with pytest.raises(ValueError):
        b.acquire_inbound(wire)
    with pytest.raises(ValueError):
        b.admit_inbound(active, 'pair')
