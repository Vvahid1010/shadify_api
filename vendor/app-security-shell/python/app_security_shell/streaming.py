"""Explicit OFF-only streams with bounded decoding and independently owned lifetime."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import time
import base64
import inspect
import zlib

import httpx
import anyio

from .transport import (
    MAX_BODY, POLICY_HEADER, TransportFailure, _acquire, _admit_current, _admit_inbound,
    _admit_egress, _json, _logical, _payload, _plain_metadata,
    _plain_response_headers,
)

CHUNK = 65536
MAX_STREAMS = 16


async def guarded(awaitable, closed):
    """Interrupt even a silent read or a blocked downstream send."""
    if closed.is_set():
        if inspect.iscoroutine(awaitable): awaitable.close()
        raise TransportFailure('outcome_unknown')
    operation = asyncio.ensure_future(awaitable)
    stopped = asyncio.create_task(closed.wait())
    try:
        done, _ = await asyncio.wait((operation, stopped), return_when=asyncio.FIRST_COMPLETED)
        if operation in done:
            return await operation
        if stopped in done:
            raise TransportFailure('outcome_unknown')
        return await operation
    finally:
        for task in (operation, stopped):
            if not task.done(): task.cancel()
        # Starlette uses AnyIO level cancellation: every checkpoint would
        # otherwise cancel HTTP cleanup again after its closed flag was set.
        # Keep ownership until the cancelled read has released its connection.
        with anyio.CancelScope(shield=True):
            drained = asyncio.gather(operation, stopped, return_exceptions=True)
            cancellation = None
            while not drained.done():
                try:
                    await asyncio.shield(drained)
                except asyncio.CancelledError as exc:
                    # Raw asyncio cancellation is separate from AnyIO's scope;
                    # remember it without interrupting the same cleanup twice.
                    cancellation = exc
            drained.result()
            if cancellation is not None:
                raise cancellation


class StreamResponse:
    """Single consumer; explicit close is required even when iteration is abandoned."""
    def __init__(self, response, headers, source, native, deadline, release, idle_timeout=60):
        self.status_code = response.status_code
        self.headers = httpx.Headers(headers)
        self.request = response.request
        self._response, self._source, self._native = response, source, native
        self._deadline, self._release = deadline, release
        self._idle_timeout, self._last_activity = idle_timeout, time.monotonic()
        self.closed = asyncio.Event()
        self.aborted = asyncio.Event()
        self._closing = None
        self._upstream_closing = None
        self._serving = 0
        self._iterated = False
        self._monitor = asyncio.create_task(self._watch())

    async def _watch(self):
        try:
            while not self.closed.is_set():
                _admit_current(self._source, self._native)
                remaining = min(self._deadline, self._last_activity + self._idle_timeout) - time.monotonic()
                if remaining <= 0: break
                await asyncio.sleep(min(.05, remaining))
        except (ValueError, RuntimeError):
            pass
        finally:
            if not self.closed.is_set():
                self.aborted.set()
                with suppress(Exception):
                    if self._serving: await self._close_upstream()
                    else: await self.aclose()

    async def _close_upstream(self):
        async def close():
            async with asyncio.timeout(2): await self._response.aclose()
        if self._upstream_closing is None:
            self._upstream_closing = asyncio.create_task(close())
        await asyncio.shield(self._upstream_closing)

    async def _close(self):
        self.closed.set()
        try:
            await self._close_upstream()
        finally:
            if self._release is not None:
                release, self._release = self._release, None
                release()

    async def aclose(self, *, graceful=False):
        if not graceful: self.aborted.set()
        if self._closing is None: self._closing = asyncio.create_task(self._close())
        await asyncio.shield(self._closing)

    async def run(self, awaitable):
        self._serving += 1
        try:
            return await guarded(awaitable, self.aborted)
        finally:
            self._serving -= 1
            await self.aclose(graceful=True)

    async def aiter_bytes(self):
        if self._iterated: raise RuntimeError('stream_already_consumed')
        self._iterated = True
        encoding = self._response.headers.get('content-encoding')
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == 'gzip' else None
        compressed = decoded = 0
        iterator = self._response.aiter_raw().__aiter__()
        try:
            while True:
                try: raw = await guarded(anext(iterator), self.aborted)
                except StopAsyncIteration: break
                # A malicious transport cannot hand us an arbitrarily large chunk.
                if len(raw) > MAX_BODY: raise ValueError('stream_chunk_limit')
                if raw: self._last_activity = time.monotonic()
                compressed += len(raw)
                for offset in range(0, len(raw), CHUNK):
                    pending = raw[offset:offset + CHUNK]
                    while pending:
                        if decoder:
                            chunk = decoder.decompress(pending, CHUNK)
                            pending = decoder.unconsumed_tail
                            if decoder.unused_data: raise ValueError('gzip_trailing_data')
                        else: chunk, pending = pending, b''
                        decoded += len(chunk)
                        if decoded > max(MAX_BODY, compressed * 100):
                            raise ValueError('gzip_expansion_limit')
                        _admit_current(self._source, self._native)
                        if self.closed.is_set(): raise TransportFailure('outcome_unknown')
                        if chunk: yield chunk
            if decoder and not decoder.eof: raise ValueError('gzip_truncated')
        except asyncio.CancelledError: raise
        except TransportFailure: raise
        except Exception: raise TransportFailure('outcome_unknown') from None
        finally:
            if self._serving: await self._close_upstream()
            else: await self.aclose(graceful=True)


async def _open_stream(client, *, connection, base_url, path, query=b'', headers=None,
                       deadline, opening_deadline, idle_timeout, release, progress, holder):
    """Open one declared GET stream; native policy remains the sole mode authority."""
    sent = False
    response = None
    try:
        remaining = opening_deadline - time.monotonic()
        if not 0 < remaining <= 3600: raise ValueError('invalid_deadline')
        async with asyncio.timeout(remaining):
            native = _acquire(client.native)
            endpoint, encrypted = native.route(connection, 'GET', path)
            if encrypted or not native.response_stream(endpoint): raise ValueError('stream_not_declared')
            logical = _logical('GET', base_url, path, query, b'', headers, remaining)
            payload = _payload('GET', path, query, logical.headers.raw, b'')
            await client.jobs.run(native.validate_outbound_plain, connection, endpoint, _json(payload))
            metadata = native.plain_policy(connection, endpoint)
            _admit_egress(client.trusted_egress, native, connection, base_url, client.http_client)
            _admit_current(client.native, native)
            wire = httpx.Request('GET', logical.url, headers=list(logical.headers.raw) +
                                [(POLICY_HEADER, metadata.encode()), (b'accept-encoding', b'identity')],
                                extensions=logical.extensions)
            # Opening has its own absolute bound. Subsequent socket reads use
            # an idle budget while StreamResponse retains authority expiry.
            wire.extensions['timeout']['read'] = idle_timeout
            sent = progress[0] = True
            response = await client.http_client.send(wire, auth=None, stream=True, follow_redirects=False)
            if _plain_metadata(response.headers.raw) != metadata: raise ValueError('policy_mismatch')
            encodings = response.headers.get_list('content-encoding')
            if encodings not in ([], ['gzip']): raise ValueError('unsupported_encoding')
            admitted_headers = [(k, v) for k, v in _plain_response_headers(response.headers.raw)
                                if k.lower() not in (b'content-encoding', b'content-length', b'connection', b'transfer-encoding')]
            head = _payload('GET', path, query, admitted_headers, b'', response.status_code)
            await client.jobs.run(native.validate_plain_response, connection, endpoint, metadata, _json(head))
            _admit_current(client.native, native)
            is_sse = response.headers.get('content-type', '').split(';')[0].strip().lower() == 'text/event-stream'
            if is_sse and response.status_code != 200: raise ValueError('invalid_stream_status')
            if not is_sse:
                if encodings: raise ValueError('encoded_bounded_error')
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > MAX_BODY: raise ValueError('response_limit')
                    body.extend(chunk)
                result = _payload('GET', path, query, admitted_headers, bytes(body), response.status_code)
                await client.jobs.run(native.validate_plain_response, connection, endpoint, metadata, _json(result))
                await response.aclose()
                response = httpx.Response(response.status_code, headers=admitted_headers,
                                          stream=httpx.ByteStream(bytes(body)), request=logical)
            holder[0] = StreamResponse(response, admitted_headers, client.native, native, deadline, release, idle_timeout)
            return holder[0]
    except asyncio.CancelledError: raise
    except Exception as exc:
        reason = 'timeout' if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else getattr(exc,'reason',None)
        raise TransportFailure('outcome_unknown' if sent else 'not_sent', reason=reason) from None
    finally:
        # Ownership transfers only on success.
        import sys
        if sys.exc_info()[0] is not None:
            try:
                if response is not None:
                    with suppress(Exception):
                        async with asyncio.timeout(2): await response.aclose()
            finally:
                if release is not None: release()


async def serve_stream(middleware, native, endpoint, metadata, payload, inner,
                       receive, send, lifetime, *, connection_id,
                       opening_timeout=30, idle_timeout=60):
    """Receiver-side OFF stream. Never dispatch before native/peer admission."""
    closed = asyncio.Event()
    disconnected = asyncio.Event()
    delivered = False
    started = finished = False
    status = None
    headers = []
    buffered = bytearray()
    sse = False
    opened = False
    created = last_activity = time.monotonic()

    async def inner_receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            from .transport import _bytes
            return {'type': 'http.request', 'body': _bytes(payload['body_b64']), 'more_body': False}
        await disconnected.wait()
        return {'type': 'http.disconnect'}

    async def disconnect():
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                disconnected.set(); closed.set(); return
            # Request body was already consumed. Any additional bytes are invalid.
            if message.get('body') or message.get('more_body'):
                closed.set(); return
            await asyncio.sleep(0)

    async def watch():
        deadline = created + lifetime
        try:
            while True:
                remaining = min(deadline, last_activity + idle_timeout if opened else created + opening_timeout) - time.monotonic()
                if remaining <= 0: break
                _admit_inbound(middleware.native, native, connection_id)
                await asyncio.sleep(min(.05, remaining))
        except (ValueError, RuntimeError): pass
        finally: closed.set()

    async def capture(message):
        nonlocal started, finished, status, headers, sse, opened, last_activity
        kind = message['type']
        if kind == 'http.response.start':
            if status is not None: raise ValueError('double_response')
            status, headers = message['status'], list(message.get('headers', []))
            if any(k.lower() in (b'connection',b'transfer-encoding',b'host',b'upgrade',b'trailer',b'te',b'keep-alive') for k,v in headers):
                raise ValueError('forbidden_stream_headers')
            view = httpx.Headers(headers)
            sse = view.get('content-type', '').split(';')[0].strip().lower() == 'text/event-stream'
            encodings = view.get_list('content-encoding')
            if encodings not in ([], ['gzip']) or (encodings and not sse):
                raise ValueError('unsupported_encoding')
            logical_headers = [(k,v) for k,v in headers if k.lower() not in (b'content-encoding', b'content-length')]
            head = _payload('GET', payload['path'], base64.b64decode(payload['query_b64']), logical_headers, b'', status)
            await middleware.jobs.run(native.validate_response_payload, endpoint, _json(head))
            _admit_inbound(middleware.native, native, connection_id)
            if sse:
                if status != 200: raise ValueError('invalid_stream_status')
                headers = [(k,v) for k,v in headers if k.lower()!=b'content-length']
                started = True
                await send({'type': 'http.response.start', 'status': status,
                            'headers': headers + [(POLICY_HEADER, metadata.encode())]})
                opened = True
                last_activity = time.monotonic()
        elif kind == 'http.response.body':
            if status is None or finished: raise ValueError('invalid_response_order')
            part = message.get('body', b'')
            if len(part) > MAX_BODY: raise ValueError('response_chunk_limit')
            _admit_inbound(middleware.native, native, connection_id)
            if sse:
                for offset in range(0, len(part), CHUNK):
                    await send({'type':'http.response.body','body':part[offset:offset+CHUNK], 'more_body':True})
                    last_activity = time.monotonic()
                if not message.get('more_body',False):
                    finished = True
                    await send({'type':'http.response.body','body':b'', 'more_body':False})
            else:
                if len(buffered) + len(part)>MAX_BODY: raise ValueError('response_limit')
                buffered.extend(part)
                if not message.get('more_body',False):
                    finished = True
                    from .transport import _bytes
                    result = _payload('GET', payload['path'], _bytes(payload['query_b64']), headers, bytes(buffered), status)
                    await middleware.jobs.run(native.validate_response_payload, endpoint, _json(result))
                    started = True
                    await send({'type':'http.response.start','status':status,'headers':headers+[(POLICY_HEADER,metadata.encode())]})
                    await send({'type':'http.response.body','body':bytes(buffered)})
        else: raise ValueError('unsupported_response')

    monitors = [asyncio.create_task(disconnect()), asyncio.create_task(watch())]
    try:
        await guarded(middleware.app(inner, inner_receive, capture), closed)
        if not finished: raise ValueError('incomplete_response')
    finally:
        for task in monitors: task.cancel()
        await asyncio.gather(*monitors, return_exceptions=True)


async def open_stream(client, *, connection, base_url, path, query=b'', headers=None,
                      deadline, release=None, open_timeout=30, idle_timeout=60):
    """Low-level owner cap plus revocation monitoring during opening and reading."""
    count = getattr(client, '_stream_count', 0)
    if count >= MAX_STREAMS:
        if release is not None: release()
        raise TransportFailure('not_sent')
    client._stream_count = count + 1
    released = False
    def done():
        nonlocal released
        if not released:
            released = True
            client._stream_count -= 1
            if release is not None: release()
    stopped = asyncio.Event()
    progress = [False, None]
    holder = [None]
    watcher = None
    result = None
    try:
        for value in (open_timeout, idle_timeout):
            if type(value) not in (int, float) or not 0 < value <= 3600: raise ValueError('invalid_stream_timeout')
        if not 0 < deadline - time.monotonic() <= 3600: raise ValueError('invalid_deadline')
        opening_deadline = min(deadline, time.monotonic() + open_timeout)
        selected = _acquire(client.native)
        async def watch_open():
            try:
                while True:
                    _admit_current(client.native, selected)
                    remaining = opening_deadline - time.monotonic()
                    if remaining <= 0:
                        progress[1] = "timeout"
                        break
                    await asyncio.sleep(min(.05, remaining))
            except (ValueError, RuntimeError): pass
            finally: stopped.set()
        watcher = asyncio.create_task(watch_open())
        result = await guarded(_open_stream(client, connection=connection, base_url=base_url,
                               path=path,query=query,headers=headers,deadline=deadline,
                               opening_deadline=opening_deadline,idle_timeout=idle_timeout,
                               release=done,progress=progress,holder=holder), stopped)
        _admit_current(client.native, selected)
        return result
    except asyncio.CancelledError:
        if holder[0] is not None:
            with suppress(Exception): await holder[0].aclose()
        else: done()
        raise
    except Exception as exc:
        if holder[0] is not None:
            with suppress(Exception): await holder[0].aclose()
        else: done()
        raise TransportFailure('outcome_unknown' if progress[0] else 'not_sent',
                               reason=getattr(exc,'reason',None) or progress[1]) from None
    finally:
        if watcher is not None:
            watcher.cancel()
            try:
                await asyncio.gather(watcher, return_exceptions=True)
            except asyncio.CancelledError:
                if holder[0] is not None:
                    with suppress(Exception): await holder[0].aclose()
                else: done()
                raise
