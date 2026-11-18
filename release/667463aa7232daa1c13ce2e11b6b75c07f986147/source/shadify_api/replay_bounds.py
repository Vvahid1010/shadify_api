"""Application-owned observations of the local clock and replay Redis.

No controller lease, selection file, Node Agent API or background worker.
RedisReplayStore continues to own the continuity marker and recovery interval.
"""
import math
import os
import subprocess

from redis import Redis
from node_agent_local_shell_transport.managed_bounds import ClockContinuity


def synchronized(output):
    fields = dict(line.split(':', 1) for line in output.splitlines() if ':' in line)
    fields = {key.strip(): value.strip() for key, value in fields.items()}
    try:
        if fields['Leap status'] != 'Normal' or not 1 <= int(fields['Stratum']) <= 15:
            return False
        if fields['Reference ID'].split()[0] in {'00000000', '0.0.0.0'}:
            return False
        offset, delay, dispersion, skew = (
            float(fields[name].split()[0]) for name in
            ('System time', 'Root delay', 'Root dispersion', 'Skew')
        )
        return (all(math.isfinite(v) for v in (offset, delay, dispersion, skew))
                and dispersion >= 0 and skew >= 0
                and abs(offset) + abs(delay) / 2 + dispersion + skew * 1e-6 * 10 <= .4)
    except (KeyError, ValueError, IndexError):
        return False


class LocalReplayBounds:
    def __init__(self, redis_url):
        self.client = Redis.from_url(redis_url, socket_connect_timeout=1,
                                     socket_timeout=1, decode_responses=True)
        self.clock_guard = None
        self.redis_run = None

    def clock_ready(self):
        try:
            if self.clock_guard is None:
                self.clock_guard = ClockContinuity()
            if not self.clock_guard.unchanged():
                return False
            result = subprocess.run(['/usr/bin/chronyc', '-n', 'tracking'],
                                    capture_output=True, text=True, timeout=1, check=False,
                                    env={**os.environ, 'LC_ALL': 'C'})
            return result.returncode == 0 and synchronized(result.stdout) and self.clock_guard.unchanged()
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def storage_ready(self):
        try:
            value = self.client.info()
            run = value.get('run_id', '')
            evicted = value.get('evicted_keys')
            if (type(evicted) is not int or evicted < 0 or len(run) != 40 or any(c not in '0123456789abcdef' for c in run)
                    or value.get('role') != 'master' or value.get('connected_slaves') != 0
                    or value.get('cluster_enabled') != 0 or value.get('loading') != 0
                    or value.get('aof_enabled') != 0 or value.get('maxmemory_policy') != 'noeviction'
                    or value.get('maxmemory', 0) <= 0):
                self.redis_run = None
                return False
            epoch = (run, evicted)
            if epoch != self.redis_run:
                self.redis_run = epoch
                return False  # Invalidate a warmed store after Redis replacement.
            return True
        except Exception:
            self.redis_run = None
            return False

    def close(self):
        if self.clock_guard is not None:
            self.clock_guard.close()
        self.client.close()
        self.redis_run = None
