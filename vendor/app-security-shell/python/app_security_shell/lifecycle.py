"""Explicit process-local snapshot installation; no public management endpoint."""
from threading import RLock
import time
from ._native import NativeContext, inbound_selector


class ContextRegistry:
    def __init__(self, *, clock=time.monotonic):
        self._lock = RLock()
        self._clock = clock
        self._overlap = {}
        self._active = None
        self._prepared = None
        self._identity = None
        self._revisions = {}
        self._generations = {}
        self._revoked = set()
        self._state = 'Not configured'
        self._receiver_routes = set()

    def prepare(self, directory, config, catalog):
        # File I/O and cryptography are completed before the state lock.
        candidate = NativeContext(directory, config, catalog)
        with self._lock:
            self._check(candidate)
            self._receiver_routes.update(candidate.receiver_routes())
            self._prepared = candidate
            if self._active is None:
                self._state = 'Prepared'
        return candidate

    def _check(self, candidate):
        app, instance, _ = candidate.status()
        if self._state == 'Draining' or (self._identity is not None and self._identity != (app, instance)):
            raise ValueError('context_rejected')
        for connection, revision, digest in candidate.revisions():
            old = self._revisions.get(connection)
            if old is not None and (revision < old[0] or (revision == old[0] and old[1] and digest != old[1])):
                raise ValueError('context_rollback_rejected')
        for instance, generation in candidate.generations():
            if (instance, generation) in self._revoked or generation < self._generations.get(instance, 0):
                raise ValueError('generation_rejected')

    def install(self, candidate, *, overlap=()):
        with self._lock:
            if candidate is not self._prepared:
                raise ValueError('unprepared_context')
            self._check(candidate)
            if not isinstance(overlap, (tuple, list)) or len(overlap) > 64:
                raise ValueError('overlap_limit')
            requested = tuple(overlap)
            if len(set(requested)) != len(requested):
                raise ValueError('duplicate_overlap')
            current = self._active
            selectors = dict(candidate.inbound_selectors())
            retained = {}
            # Policy removal/change revokes old admission immediately, including
            # when the caller did not request another rotation.
            for connection, entry in self._overlap.items():
                old, selector, since = entry
                if connection in selectors and old.rotation_authorization(connection) == candidate.rotation_authorization(connection):
                    if current is None or dict(current.inbound_selectors()).get(connection) != selectors[connection]:
                        raise ValueError('overlap_not_retired')
                    retained[connection] = entry
            for connection in requested:
                if current is None or connection in self._overlap:
                    raise ValueError('overlap_not_available')
                old_selector = dict(current.inbound_selectors()).get(connection)
                if old_selector is None or connection not in selectors or old_selector == selectors[connection]:
                    raise ValueError('rotation_required')
                if current.rotation_authorization(connection) != candidate.rotation_authorization(connection):
                    raise ValueError('rotation_policy_changed')
                # A revision-only edit is not a key rotation.
                if current.connection_generations(connection) == candidate.connection_generations(connection):
                    raise ValueError('rotation_required')
                retained[connection] = (current, old_selector, None)
            incoming = {c: (r, d) for c, r, d in candidate.revisions()}
            # Removed connections require a newer revision for future reenrollment.
            if self._active is not None:
                for connection, revision, digest in self._active.revisions():
                    if connection not in incoming:
                        self._revisions[connection] = (revision + 1, '')
            self._revisions.update(incoming)
            for instance, generation in candidate.generations():
                self._generations[instance] = max(generation, self._generations.get(instance, 0))
            app, instance, _ = candidate.status()
            self._identity = (app, instance)
            self._active, self._prepared = candidate, None
            self._state = 'Active'
            self._overlap = retained

    def checkpoint(self):
        # Public observation only. The owning operation must bind this to its
        # approved publication and current durable deny/floor state.
        with self._lock:
            if self._identity is None or self._state not in {'Active', 'Blocked'}:
                raise ValueError('checkpoint_unavailable')
            return {
                'schema_version': 1,
                'identity': list(self._identity),
                'receiver_routes': [list(row) for row in sorted(self._receiver_routes)],
                'active': None if self._active is None else {
                    'consumed': list(self._active.status()),
                    'snapshot_sha256': self._active.snapshot_digest(),
                    'revisions': [list(row) for row in self._active.revisions()]},
                'revisions': [[c, r, d] for c, (r, d) in sorted(self._revisions.items())],
                'generations': [list(row) for row in sorted(self._generations.items())],
                'revoked': [list(row) for row in sorted(self._revoked)],
                'overlap': {c: self.overlap_status(c) for c in sorted(self._overlap)}
                    if self._active is not None else {},
            }

    def restore(self, candidate, *, checkpoint, overlaps):
        from .restoration import reconstruct
        with self._lock:
            if self._active is not None or self._state not in {'Not configured', 'Prepared'}:
                raise ValueError('restore_requires_fresh_registry')
            if type(candidate) is NativeContext:
                self._receiver_routes.update(candidate.receiver_routes())
            try:
                restored = reconstruct(self, candidate, checkpoint, overlaps)
                if not self._revoked <= restored._revoked:
                    raise ValueError('restore_revocation_changed')
                if candidate is not None:
                    self._check(candidate)
                for connection, (revision, digest) in self._revisions.items():
                    incoming = restored._revisions.get(connection)
                    if incoming is None or incoming[0] < revision or (incoming[0] == revision and digest and incoming[1] != digest):
                        raise ValueError('restore_floor_changed')
                if any(restored._generations.get(i, 0) < g for i, g in self._generations.items()):
                    raise ValueError('restore_floor_changed')
            except Exception:
                self._prepared = None
                self._state = 'Draining'  # no automatic install fallback
                raise
            for field in ('_active', '_prepared', '_identity', '_revisions',
                          '_generations', '_revoked', '_state', '_receiver_routes', '_overlap'):
                setattr(self, field, getattr(restored, field))

    def receiver_routes(self):
        with self._lock:
            return tuple(self._receiver_routes)

    def acquire(self):
        with self._lock:
            if self._state != 'Active' or self._active is None:
                raise ValueError('context_not_active')
            return self._active

    def acquire_inbound(self, wire):
        connection, selector = inbound_selector(wire)
        with self._lock:
            active = self.acquire()
            if dict(active.inbound_selectors()).get(connection) == selector:
                return active
            old = self._overlap.get(connection)
            if old is not None and old[1] == selector:
                return old[0]
            raise ValueError('inbound_context_rejected')

    def acquire_plain(self, connection, metadata):
        with self._lock:
            active = self.acquire()
            if active.inbound_plain_matches(connection, metadata):
                return active
            old = self._overlap.get(connection)
            if old is not None and old[0].inbound_plain_matches(connection, metadata):
                return old[0]
            raise ValueError('inbound_context_rejected')

    def admit_inbound(self, selected, connection):
        with self._lock:
            active = self.acquire()
            old = self._overlap.get(connection)
            if selected is not active and (old is None or old[0] is not selected):
                raise ValueError('context_changed')

    def overlap_status(self, connection):
        with self._lock:
            active = self.acquire()
            old = self._overlap.get(connection)
            if old is None:
                raise ValueError('overlap_not_available')
            return {'expected_old': old[1],
                    'expected_current': dict(active.inbound_selectors())[connection]}

    def _overlap_expected(self, connection, expected_old, expected_current):
        if self.overlap_status(connection) != {
                'expected_old': expected_old, 'expected_current': expected_current}:
            raise ValueError('stale_overlap')

    def mark_cutover(self, connection, *, expected_old, expected_current):
        # Called by the owning governed operation after consumer cutover proof.
        # Every subsequent consumer proof extends the minimum retention window.
        with self._lock:
            self._overlap_expected(connection, expected_old, expected_current)
            old = self._overlap.get(connection)
            if old is None:
                raise ValueError('overlap_not_available')
            now = self._clock()
            if old[2] is not None and now < old[2]:
                raise ValueError('cutover_clock_rejected')
            self._overlap[connection] = (old[0], old[1], now)

    def retire_overlap(self, connection, *, expected_old, expected_current):
        with self._lock:
            self._overlap_expected(connection, expected_old, expected_current)
            old = self._overlap.get(connection)
            if old is None or old[2] is None or self._clock() - old[2] < 211:
                raise ValueError('overlap_retirement_rejected')
            del self._overlap[connection]

    def revoke(self, instance, generation):
        with self._lock:
            self._revoked.add((instance, generation))
            self._overlap = {c: entry for c, entry in self._overlap.items()
                             if (instance, generation) not in entry[0].generations()}
            if self._active is not None and (instance, generation) in self._active.generations():
                self._active = None
                self._state = 'Blocked'
            if self._prepared is not None and (instance, generation) in self._prepared.generations():
                self._prepared = None

    def status(self):
        with self._lock:
            return {'state': self._state, 'consumed': self._active.status() if self._active is not None else None,
                    'connection_revisions': self._active.revisions() if self._active is not None else []}

    def drain(self):
        with self._lock:
            self._state = 'Draining'
            self._overlap.clear()
            self._active = self._prepared = None
        # Existing admitted tickets hold their immutable native context; the
        # owning adapter drains NativeJobs before release/shutdown. No lock wait.
