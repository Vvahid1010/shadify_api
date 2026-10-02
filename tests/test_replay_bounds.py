"""Synthetic boundary failures; no chronyc process or Redis contact."""
import unittest
from unittest.mock import Mock,patch
from shadify_api.replay_bounds import LocalReplayBounds,synchronized

TRACKING="""Leap status : Normal
Stratum : 2
Reference ID : ABCDEF12
System time : 0.001 seconds fast
Root delay : 0.002 seconds
Root dispersion : 0.003 seconds
Skew : 1.0 ppm
"""

class BoundsTests(unittest.TestCase):
    def test_clock_requires_synchronized_finite_bounded_error(self):
        self.assertTrue(synchronized(TRACKING))
        for before,after in (("Normal","Not synchronised"),("0.001","nan"),("0.001","0.5"),("Stratum : 2","Stratum : 0")):
            self.assertFalse(synchronized(TRACKING.replace(before,after)))

    def test_kernel_clock_step_denies_without_chronyc(self):
        with patch("shadify_api.replay_bounds.Redis.from_url"):
            bounds=LocalReplayBounds("redis://:synthetic@127.0.0.1")
        bounds.clock_guard=Mock();bounds.clock_guard.unchanged.return_value=False
        with patch("shadify_api.replay_bounds.subprocess.run") as run:
            self.assertFalse(bounds.clock_ready());run.assert_not_called()
        bounds.close()

    def test_replay_replacement_eviction_and_replica_cannot_reuse_warm_bounds(self):
        info=dict(run_id="a"*40,evicted_keys=0,role="master",connected_slaves=0,cluster_enabled=0,
                  loading=0,aof_enabled=0,maxmemory_policy="noeviction",maxmemory=1048576)
        with patch("shadify_api.replay_bounds.Redis.from_url") as redis:
            bounds=LocalReplayBounds("redis://:synthetic@127.0.0.1")
            redis.return_value.info.return_value=info
            self.assertFalse(bounds.storage_ready());self.assertTrue(bounds.storage_ready())
            info["run_id"]="b"*40
            self.assertFalse(bounds.storage_ready());self.assertTrue(bounds.storage_ready())
            info["evicted_keys"]=1
            self.assertFalse(bounds.storage_ready());self.assertTrue(bounds.storage_ready())
            info["connected_slaves"]=1
            self.assertFalse(bounds.storage_ready())
            bounds.close()

if __name__=="__main__":unittest.main()
