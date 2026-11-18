"""Bound native work across callers/loops; retain running buffers on cancellation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import RLock, Event


class NativeJobs:
    def __init__(self, active=4, queued=16):
        if type(active) is not int or type(queued) is not int or not 1 <= active <= 16 or not 0 <= queued <= 64:
            raise ValueError('invalid_native_limits')
        self._executor = ThreadPoolExecutor(max_workers=active, thread_name_prefix='app-security')
        self._capacity = active + queued
        self._tasks = set()
        self._lock = RLock()
        self._closed = False

    def _submit(self, function, args):
        with self._lock:
            if self._closed or len(self._tasks) >= self._capacity:
                raise RuntimeError('native_queue_unavailable')
            cancelled = Event()
            def execute():
                if cancelled.is_set():
                    return None
                return function(*args)
            future = self._executor.submit(execute)
            self._tasks.add(future)
            def finished(done):
                with self._lock:
                    self._tasks.discard(done)
                if not done.cancelled():
                    done.exception()  # Retrieve without printing sensitive context.
            future.add_done_callback(finished)
            return future, cancelled

    async def run(self, function, *args):
        future, cancelled = self._submit(function, args)
        wrapped = asyncio.wrap_future(future)
        # Retrieve a late exception even if the awaiting request was cancelled.
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            cancelled.set()  # Keep capacity charged until the worker dequeues this item.
            raise

    def run_sync(self, function, *args, timeout=None):
        future, cancelled = self._submit(function, args)
        try:
            return future.result(timeout=timeout)
        except BaseException:
            cancelled.set()
            raise

    async def close(self):
        with self._lock:
            self._closed = True
            tasks = tuple(self._tasks)
        await asyncio.gather(*(asyncio.wrap_future(task) for task in tasks), return_exceptions=True)
        self._executor.shutdown(wait=False)
