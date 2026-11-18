"""Atomic replay admission with explicit continuity and trusted-clock gates."""
from __future__ import annotations
import re
import secrets
import time


class ReplayRejected(Exception):
    pass


_RESERVE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return -1 end
if redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[2]) then return 1 end
return 0
"""


class RedisReplayStore:
    def __init__(self, client, namespace: str, clock_bounds, *, storage_bounds=lambda: False, monotonic=time.monotonic):
        if not re.fullmatch(r'app-security:replay:[a-zA-Z0-9_-]{1,64}', namespace):
            raise ValueError('invalid_replay_namespace')
        self._client = client
        self._namespace = namespace
        self._clock_bounds = clock_bounds
        self._monotonic = monotonic
        self._storage_bounds = storage_bounds
        self._epoch = 0
        self._token = None
        self._healthy_since = None

    def invalidate(self):
        self._epoch += 1
        self._token = None
        self._healthy_since = None

    def check_bounds(self):
        # A health observation may fail as well as return false. Neither may
        # leave a warmed store usable or leak an observer exception to callers.
        try:
            healthy = self._clock_bounds() is True and self._storage_bounds() is True
        except Exception:
            healthy = False
        if not healthy:
            self.invalidate()
            raise ReplayRejected('clock_bounds_unavailable') from None

    async def reserve(self, digest: str, expires_at: int, now: int):
        if not re.fullmatch(r'[0-9a-f]{64}', digest) or type(now) is not int or type(expires_at) is not int or not now < expires_at <= now + 90:
            raise ReplayRejected('replay_rejected')
        marker = f'{self._namespace}:continuity'
        try:
            self.check_bounds()
            token = await self._client.get(marker)
            if token is None:
                await self._client.set(marker, secrets.token_hex(32), nx=True)
                token = await self._client.get(marker)
            if isinstance(token, bytes):
                token = token.decode('ascii')
            if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token):
                self.invalidate()
                raise ReplayRejected('replay_continuity_unavailable')
            if token != self._token:
                self._epoch += 1
                self._token = token
                self._healthy_since = self._monotonic()
            if self._healthy_since is None or self._monotonic() - self._healthy_since < 151:
                raise ReplayRejected('replay_recovery_wait')
            epoch = self._epoch
            result = await self._client.eval(_RESERVE, 2, marker, f'{self._namespace}:{digest}', token, expires_at - now + 61)
            # EVAL can suspend across a clock step, expired health lease or
            # resource loss. Recheck before permitting business dispatch. The
            # reservation is intentionally retained even when this check denies.
            self.check_bounds()
            if epoch != self._epoch:
                raise ReplayRejected("replay_continuity_unavailable")
            if result == -1:
                self.invalidate()
                raise ReplayRejected('replay_continuity_unavailable')
            if result != 1:
                raise ReplayRejected('replay_rejected')
        except ReplayRejected:
            raise
        except Exception:
            self.invalidate()
            raise ReplayRejected('replay_unavailable') from None
        # Deliberately no release API: unknown business outcomes remain reserved.
