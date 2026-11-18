"""Real task-owned Redis, private socket only, no application credentials."""
import asyncio
import multiprocessing
from pathlib import Path
import subprocess
import time
import uuid

import pytest
import redis
import redis.asyncio

from app_security_shell.replay import RedisReplayStore, ReplayRejected


@pytest.fixture
def isolated_redis(tmp_path):
    # Linux socket paths are bounded; use a short task-owned directory name.
    root=Path('.build-tools/test-runs') / ('r-' + uuid.uuid4().hex[:8])
    root.mkdir(mode=0o700)
    socket=root / 'redis.sock'
    process=subprocess.Popen(['/usr/bin/redis-server','--port','0','--unixsocket',str(socket.absolute()),
        '--unixsocketperm','700','--save','','--appendonly','no','--dir',str(root.absolute())],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    client=redis.Redis(unix_socket_path=str(socket.absolute()))
    try:
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if process.poll() is not None: pytest.fail('isolated Redis exited')
            try:
                if client.ping(): break
            except redis.ConnectionError: time.sleep(.02)
        else: pytest.fail('isolated Redis did not become ready')
        yield str(socket.absolute())
    finally:
        client.close()
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)
        # Exact task-owned files only; never flush/stop managed Redis.
        socket.unlink(missing_ok=True)
        root.rmdir()


def test_atomicity_expiry_continuity_and_clock_gate(isolated_redis):
    async def run():
        client=redis.asyncio.Redis(unix_socket_path=isolated_redis)
        mono=[0.0];healthy=[True]
        store=RedisReplayStore(client,'app-security:replay:fixture',lambda:healthy[0],storage_bounds=lambda:True,monotonic=lambda:mono[0])
        with pytest.raises(ReplayRejected,match='recovery_wait'):
            await store.reserve('a'*64,160,100)
        mono[0]=151
        results=await asyncio.gather(*(store.reserve('a'*64,160,100) for _ in range(12)),return_exceptions=True)
        assert sum(r is None for r in results)==1
        assert await client.ttl('app-security:replay:fixture:'+'a'*64) in (120,121)
        # There is no release on handler failure/cancellation.
        with pytest.raises(ReplayRejected): await store.reserve('a'*64,160,100)
        await client.delete('app-security:replay:fixture:continuity')
        with pytest.raises(ReplayRejected,match='recovery_wait'): await store.reserve('b'*64,160,100)
        mono[0]=301
        with pytest.raises(ReplayRejected,match='recovery_wait'): await store.reserve('b'*64,160,100)
        mono[0]=302
        await store.reserve('b'*64,160,100)
        healthy[0]=False
        with pytest.raises(ReplayRejected,match='clock_bounds'): await store.reserve('c'*64,160,100)
        healthy[0]=True
        with pytest.raises(ReplayRejected,match='recovery_wait'): await store.reserve('c'*64,160,100)
        await client.aclose()
    asyncio.run(run())


def _contender(socket, barrier, result):
    async def run():
        client=redis.asyncio.Redis(unix_socket_path=socket)
        mono=[0.0]
        store=RedisReplayStore(client,'app-security:replay:multiprocess',lambda:True,storage_bounds=lambda:True,monotonic=lambda:mono[0])
        try: await store.reserve('f'*64,160,100)
        except ReplayRejected: pass
        mono[0]=151
        await asyncio.to_thread(barrier.wait,5)
        try:
            await store.reserve('f'*64,160,100)
            result.put(True)
        except ReplayRejected: result.put(False)
        finally: await client.aclose()
    asyncio.run(run())


def test_multiprocess_single_admission(isolated_redis):
    ctx=multiprocessing.get_context('spawn')
    barrier=ctx.Barrier(3);result=ctx.Queue()
    children=[ctx.Process(target=_contender,args=(isolated_redis,barrier,result)) for _ in range(3)]
    try:
        for child in children: child.start()
        outcomes=[result.get(timeout=15) for _ in children]
        assert sum(outcomes)==1
        for child in children:
            child.join(timeout=5)
            assert child.exitcode==0
    finally:
        for child in children:
            if child.is_alive(): child.terminate();child.join(timeout=5)
        result.close();result.join_thread()


def test_invalidation_during_reservation_denies_dispatch(isolated_redis):
    async def run():
        client=redis.asyncio.Redis(unix_socket_path=isolated_redis)
        mono=[0.0]
        store=RedisReplayStore(client,'app-security:replay:race',lambda:True,storage_bounds=lambda:True,monotonic=lambda:mono[0])
        with pytest.raises(ReplayRejected): await store.reserve('a'*64,160,100)
        mono[0]=151
        original=client.eval
        async def raced(*args):
            result=await original(*args)
            store.invalidate()
            return result
        client.eval=raced
        with pytest.raises(ReplayRejected,match='continuity'):
            await store.reserve('a'*64,160,100)
        assert await client.exists('app-security:replay:race:'+'a'*64)==1
        await client.aclose()
    asyncio.run(run())


def test_observed_continuity_transition_invalidates_pending_admission(isolated_redis):
    async def run():
        client=redis.asyncio.Redis(unix_socket_path=isolated_redis)
        assert (await client.config_get('maxmemory-policy'))['maxmemory-policy']=='noeviction'
        mono=[0.0]
        store=RedisReplayStore(client,'app-security:replay:transition',lambda:True,storage_bounds=lambda:True,monotonic=lambda:mono[0])
        with pytest.raises(ReplayRejected): await store.reserve('a'*64,160,100)
        mono[0]=151
        pending,resume=asyncio.Event(),asyncio.Event()
        original=client.eval
        async def delayed(*args):
            result=await original(*args)
            pending.set();await resume.wait()
            return result
        client.eval=delayed
        first=asyncio.create_task(store.reserve('a'*64,160,100))
        await pending.wait()
        await client.set('app-security:replay:transition:continuity','b'*64)
        with pytest.raises(ReplayRejected,match='recovery_wait'):
            await store.reserve('c'*64,160,100)
        resume.set()
        with pytest.raises(ReplayRejected,match='continuity'):
            await first
        await client.aclose()
    asyncio.run(run())


def test_connection_loss_invalidates_admission(isolated_redis):
    async def run():
        client=redis.asyncio.Redis(unix_socket_path=isolated_redis)
        unavailable=redis.asyncio.Redis(unix_socket_path=str(Path(isolated_redis).with_name('absent.sock')))
        mono=[0.0]
        store=RedisReplayStore(client,'app-security:replay:loss',lambda:True,storage_bounds=lambda:True,monotonic=lambda:mono[0])
        try:
            with pytest.raises(ReplayRejected,match='recovery_wait'):
                await store.reserve('a'*64,160,100)
            mono[0]=151
            await store.reserve('a'*64,160,100)
            store._client=unavailable
            with pytest.raises(ReplayRejected,match='replay_unavailable'):
                await store.reserve('b'*64,160,100)
            store._client=client
            with pytest.raises(ReplayRejected,match='recovery_wait'):
                await store.reserve('b'*64,160,100)
            assert not await client.exists('app-security:replay:loss:'+'b'*64)
        finally:
            await client.aclose()
            await unavailable.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('boundary', ['clock', 'storage'])
@pytest.mark.parametrize('loss', ['false', 'wrong_type', 'exception'])
def test_bounds_lost_during_atomic_reservation_deny_and_retain(isolated_redis, boundary, loss):
    async def run():
        client = redis.asyncio.Redis(unix_socket_path=isolated_redis)
        mono = [0.0]
        healthy = [True]
        def bounds():
            if healthy[0]:
                return True
            if loss == 'exception':
                raise RuntimeError('synthetic health observation failure')
            return False if loss == 'false' else 'true'
        store = RedisReplayStore(client, 'app-security:replay:post-eval',
            bounds if boundary == 'clock' else lambda: True,
            storage_bounds=bounds if boundary == 'storage' else lambda: True,
            monotonic=lambda: mono[0])
        try:
            with pytest.raises(ReplayRejected, match='recovery_wait'):
                await store.reserve('1' * 64, 160, 100)
            mono[0] = 151
            original = client.eval
            async def raced(*args):
                result = await original(*args)
                healthy[0] = False
                return result
            client.eval = raced
            with pytest.raises(ReplayRejected):
                await store.reserve('2' * 64, 160, 100)
            assert await client.get('app-security:replay:post-eval:' + '2' * 64) == b'1'
            assert store._token is None and store._healthy_since is None
            healthy[0] = True
            client.eval = original
            with pytest.raises(ReplayRejected, match='recovery_wait'):
                await store.reserve('3' * 64, 160, 100)
            mono[0] = 302
            with pytest.raises(ReplayRejected, match='replay_rejected'):
                await store.reserve('2' * 64, 160, 100)
            await store.reserve('3' * 64, 160, 100)
        finally:
            await client.aclose()
    asyncio.run(run())
