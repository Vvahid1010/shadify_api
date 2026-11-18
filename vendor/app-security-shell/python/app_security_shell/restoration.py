"""Validate an authority-supplied public checkpoint; no persistence or key cache.

The controller owns freshness, encrypted custody and publication. This module
cannot authenticate a checkpoint copied from an old backup by itself.
"""
from ._native import NativeContext

MAX_U64 = (1 << 64) - 1


def _rows(value, width, limit):
    if type(value) is not list or len(value) > limit:
        raise ValueError('restore_shape')
    result = []
    for row in value:
        if type(row) is not list or len(row) != width:
            raise ValueError('restore_shape')
        if type(row[0]) is not str or not 1 <= len(row[0]) <= 128:
            raise ValueError('restore_identity')
        if type(row[1]) is not int or not 1 <= row[1] <= MAX_U64:
            raise ValueError('restore_floor')
        if width == 3 and (type(row[2]) is not str or
                           (row[2] != '' and (len(row[2]) != 64 or any(c not in '0123456789abcdef' for c in row[2])))):
            raise ValueError('restore_digest')
        result.append(tuple(row))
    return result


def _unique(rows):
    if len({row[0] for row in rows}) != len(rows):
        raise ValueError('restore_duplicate')
    return rows


def reconstruct(registry, candidate, checkpoint, overlaps):
    """Build a detached registry; the caller publishes it atomically under lock."""
    from .lifecycle import ContextRegistry
    if type(checkpoint) is not dict or set(checkpoint) != {
        'schema_version', 'identity', 'receiver_routes', 'active', 'revisions', 'generations', 'revoked', 'overlap'
    } or type(checkpoint['schema_version']) is not int or checkpoint['schema_version'] != 1:
        raise ValueError('restore_shape')
    identity = checkpoint['identity']
    if type(identity) is not list or len(identity) != 2 or any(type(x) is not str or not 1 <= len(x) <= 128 for x in identity):
        raise ValueError('restore_identity')
    revisions = _unique(_rows(checkpoint['revisions'], 3, 4096))
    generations = _unique(_rows(checkpoint['generations'], 2, 4096))
    revoked = _rows(checkpoint['revoked'], 2, 4096)
    if len(set(revoked)) != len(revoked):
        raise ValueError('restore_duplicate')
    expected = checkpoint['overlap']
    if type(expected) is not dict or len(expected) > 64 or type(overlaps) is not dict or set(overlaps) != set(expected):
        raise ValueError('restore_overlap')
    result = ContextRegistry(clock=registry._clock)
    routes = checkpoint['receiver_routes']
    if type(routes) is not list or len(routes) > 8192 or any(
        type(row) is not list or len(row) != 2 or
        type(row[0]) is not str or row[0] not in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'} or
        type(row[1]) is not str or not row[1].startswith('/') or len(row[1]) > 4096
        for row in routes
    ):
        raise ValueError('restore_routes')
    result._receiver_routes = {tuple(row) for row in routes}
    if len(result._receiver_routes) != len(routes):
        raise ValueError('restore_duplicate')
    result._identity = tuple(identity)
    result._revisions = {c: (r, d) for c, r, d in revisions}
    result._generations = dict(generations)
    result._revoked = set(revoked)
    active = checkpoint['active']
    if active is None:
        if candidate is not None or overlaps:
            raise ValueError('restore_blocked')
        result._state = 'Blocked'
        return result
    if type(active) is not dict or set(active) != {'consumed', 'revisions', 'snapshot_sha256'} or type(candidate) is not NativeContext:
        raise ValueError('restore_active')
    if active['snapshot_sha256'] != candidate.snapshot_digest():
        raise ValueError('restore_publication_mismatch')
    consumed = active['consumed']
    if type(consumed) is not list or len(consumed) != 3 or type(consumed[2]) is not int:
        raise ValueError('restore_active')
    _unique(_rows(active['revisions'], 3, 64))
    if active['consumed'] != list(candidate.status()) or active['revisions'] != [list(row) for row in candidate.revisions()]:
        raise ValueError('restore_publication_mismatch')
    result._check(candidate)
    # Every active binding must have an exact durable floor, not just a lower
    # revision supplied alongside a newer backed-up-but-unpublished candidate.
    if any(result._revisions.get(c) != (r, d) for c, r, d in candidate.revisions()) or any(
        result._generations.get(i) != g for i, g in candidate.generations()
    ):
        raise ValueError('restore_floor_mismatch')
    if not set(candidate.receiver_routes()) <= result._receiver_routes:
        raise ValueError('restore_routes')
    current_selectors = dict(candidate.inbound_selectors())
    retained = {}
    for connection, old in overlaps.items():
        if type(old) is not NativeContext or connection not in current_selectors or old.status()[:2] != candidate.status()[:2]:
            raise ValueError('restore_overlap')
        old_selector = dict(old.inbound_selectors()).get(connection)
        token = {'expected_old': old_selector, 'expected_current': current_selectors[connection]}
        if old_selector is None or expected[connection] != token or old_selector == current_selectors[connection]:
            raise ValueError('restore_overlap_mismatch')
        if old.rotation_authorization(connection) != candidate.rotation_authorization(connection):
            raise ValueError('restore_policy_changed')
        old_revisions = {c: r for c, r, _ in old.revisions()}
        new_revisions = {c: r for c, r, _ in candidate.revisions()}
        if old_revisions[connection] >= new_revisions[connection]:
            raise ValueError('restore_revision_order')
        old_pair, new_pair = old.connection_generations(connection), candidate.connection_generations(connection)
        if old_pair == new_pair or any(a > b for a, b in zip(old_pair, new_pair)):
            raise ValueError('restore_generation')
        if any(row in result._revoked for row in old.generations()):
            raise ValueError('restore_revoked')
        retained[connection] = (old, old_selector, None)
    result._prepared = candidate
    result.install(candidate)
    result._overlap = retained
    result._receiver_routes.update(candidate.receiver_routes())
    for old, _, _ in retained.values():
        result._receiver_routes.update(old.receiver_routes())
    return result
