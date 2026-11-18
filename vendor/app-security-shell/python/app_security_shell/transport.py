"""HTTP adapters for the native protocol. No runtime crypto or plaintext fallback."""
from __future__ import annotations
import asyncio
import base64
from dataclasses import dataclass
import json
import time
from contextlib import asynccontextmanager, contextmanager
from .routing import METHODS, matches, valid_path, normalize_base

import httpx

from .jobs import NativeJobs

CARRIER = '/_app-security/v1/message'
MEDIA = 'application/app-security+json'
POLICY_HEADER = b'x-app-security-policy'
MAX_WIRE = 2 * 1024 * 1024
MAX_BODY = 1024 * 1024


class TransportFailure(Exception):
    def __init__(self, outcome, *, reason=None):
        self.outcome = outcome
        self.reason = reason
        super().__init__(f'app_security_{outcome}')


def _acquire(source):
    return source.acquire() if hasattr(source, 'acquire') else source


def _admit_current(source, selected):
    # Selection before an await is not admission. A revoked/replaced snapshot
    # cannot start new I/O/business work after body reads, replay or crypto queues.
    if _acquire(source) is not selected:
        raise ValueError('context_changed')


def _admit_inbound(source, selected, connection):
    if hasattr(source, 'admit_inbound'):
        source.admit_inbound(selected, connection)
    else:
        _admit_current(source, selected)


def _json(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def _b64(value):
    return base64.b64encode(value).decode('ascii')


def _bytes(value):
    return base64.b64decode(value, validate=True)


def _headers(headers):
    return [{'name': key.decode('ascii').lower(), 'value_b64': _b64(value)} for key, value in headers
            if key.lower() not in (b'host', b'content-length', b'connection', b'transfer-encoding', b'accept-encoding', b'user-agent')]


def _payload(method, path, query, headers, body, status=None, *, response_limit=MAX_BODY):
    if len(body) > (MAX_BODY if status is None else response_limit):
        raise TransportFailure('not_sent')
    return {'method': method, 'path': path, 'query_b64': _b64(query), 'headers': _headers(headers),
            'body_b64': _b64(body), 'status': status, 'response_to': None}


def _plain_metadata(headers):
    values = [value for name, value in headers if name.lower() == POLICY_HEADER]
    if len(values) != 1 or len(values[0]) > 1024:
        raise ValueError('invalid_plain_policy')
    return values[0].decode('ascii')


def _without_policy(headers):
    return [(name, value) for name, value in headers if name.lower() != POLICY_HEADER]


def _plain_response_headers(headers):
    # Servers/proxies add these outside the app middleware. Like ON carrier
    # headers, they are transport metadata, never authenticated business fields.
    return [(name, value) for name, value in headers
            if name.lower() not in (POLICY_HEADER, b'date', b'server')]


@dataclass(frozen=True)
class TrustedEgress:
    """Attestation supplied only by an app's trusted managed transport adapter.

    Never derive this from a URL scheme, config boolean or request headers.
    The adapter must establish the actual client/channel and destination binding.
    """
    connection_id: str
    base_url: str
    sender_node_id: str
    sender_app_instance_id: str
    recipient_node_id: str
    recipient_app_instance_id: str
    policy_revision: int
    policy_digest: str
    http_client: object


def _admit_egress(adapter, native, connection, base_url, http_client):
    base = base_url.rstrip('/')
    evidence = adapter(connection, base, http_client) if adapter is not None else None
    if not isinstance(evidence, TrustedEgress) or evidence.http_client is not http_client:
        raise ValueError('untrusted_egress')
    if evidence.connection_id != connection or evidence.base_url != base:
        raise ValueError('untrusted_destination')
    actual = (evidence.sender_node_id, evidence.sender_app_instance_id,
              evidence.recipient_node_id, evidence.recipient_app_instance_id,
              evidence.policy_revision, evidence.policy_digest)
    if actual != native.transport_binding(connection):
        raise ValueError('untrusted_connection')


def _logical(method,base,path,query,body,headers,timeout):
    if method not in METHODS or not valid_path(path) or type(body) is not bytes or len(body)>MAX_BODY:
        raise ValueError('invalid_request')
    if type(query) is not bytes or len(query)>8192 or any(b<33 or b>126 for b in query) or query.startswith(b'?') or b'#' in query:
        raise ValueError('invalid_query')
    # Header syntax and endpoint allowlists remain native-owned. Prevent framing
    # or ambient metadata from disappearing in the logical-header conversion.
    supplied=httpx.Headers(headers)
    forbidden={b'host',b'content-length',b'connection',b'transfer-encoding',b'accept-encoding',b'user-agent',b'cookie',b'x-interapp-envelope',b'app_security.admitted'}
    if any(k.lower() in forbidden or k.lower().startswith(b'x-app-security-') for k,v in supplied.raw):raise ValueError('invalid_headers')
    base=normalize_base(base)
    url=httpx.URL(base+path).copy_with(query=query or None)
    expected=(httpx.URL(base).raw_path.rstrip(b'/')+path.encode())
    if url.raw_path.split(b'?')[0]!=expected or url.query!=query:raise ValueError('invalid_target')
    return httpx.Request(method,url,headers=supplied,content=body,extensions={'timeout':httpx.Timeout(timeout).as_dict()})


def _json_input(json_body,headers):
    request=httpx.Request('POST','http://fixture',json=json_body,headers=headers)
    supplied=httpx.Headers(headers)
    if 'content-type' not in supplied and 'content-type' in request.headers:supplied['content-type']=request.headers['content-type']
    return request.content,supplied


@asynccontextmanager
async def _astream(client,request):
    response=await client.send(request,auth=None,stream=True,follow_redirects=False)
    try:yield response
    finally:await response.aclose()


@contextmanager
def _stream(client,request):
    response=client.send(request,auth=None,stream=True,follow_redirects=False)
    try:yield response
    finally:response.close()


class Client:
    def __init__(self, native, http_client, *, jobs=None, clock=time.time, trusted_egress=None):
        self.native = native
        self.http_client = http_client
        self.jobs = jobs or NativeJobs()
        self.clock = clock
        self.trusted_egress = trusted_egress

    async def post_json(self, *, connection, base_url, path, json_body, headers=None, timeout=30.0):
        try:body,headers=_json_input(json_body,headers)
        except Exception:raise TransportFailure('not_sent') from None
        return await self.request(connection=connection,base_url=base_url,method='POST',path=path,body=body,headers=headers,timeout=timeout)

    async def request(self, *, connection, base_url, method, path, query=b'', body=b'', headers=None, timeout=30.0):
        # A finite caller budget covers queue, crypto, I/O and response verification.
        if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise TransportFailure('not_sent')
        sent = False
        try:
            async with asyncio.timeout(timeout):
                native = _acquire(self.native)
                endpoint, encrypted = native.route(connection, method, path)
                if native.response_stream(endpoint): raise ValueError('use_stream_api')
                response_limit = native.max_response_bytes(endpoint)
                logical = _logical(method,base_url,path,query,body,headers,timeout)
                base_url=normalize_base(base_url)
                payload = _payload(method, path, logical.url.query, logical.headers.raw, logical.content)
                # Envelope authentication never replaces the independent channel.
                _admit_egress(self.trusted_egress, native, connection, base_url, self.http_client)
                _admit_current(self.native, native)
                if not encrypted:
                    await self.jobs.run(native.validate_outbound_plain, connection, endpoint, _json(payload))
                    metadata = native.plain_policy(connection, endpoint)
                    _admit_current(self.native, native)
                    wire_headers = list(logical.headers.raw) + [(POLICY_HEADER, metadata.encode('ascii'))]
                    sent = True
                    wire_request=httpx.Request(method,logical.url,content=logical.content,headers=wire_headers,extensions=logical.extensions)
                    async with _astream(self.http_client,wire_request) as response:
                        if response.headers.get('content-encoding'):
                            raise TransportFailure('outcome_unknown')
                        response_metadata = _plain_metadata(response.headers.raw)
                        if response_metadata != metadata:
                            raise TransportFailure('outcome_unknown')
                        response_headers = _plain_response_headers(response.headers.raw)
                        body = bytearray()
                        async for chunk in response.aiter_raw():
                            if len(body) + len(chunk) > response_limit:
                                raise TransportFailure('outcome_unknown')
                            body.extend(chunk)
                        result = _payload(method, path, logical.url.query, response_headers, bytes(body), response.status_code, response_limit=response_limit)
                        await self.jobs.run(native.validate_plain_response, connection, endpoint, response_metadata, _json(result))
                        return httpx.Response(response.status_code, content=bytes(body), headers=response_headers, request=logical)
                ticket = await self.jobs.run(native.begin, connection, endpoint, _json(payload), int(self.clock()))
                wire = ticket.wire()
                _admit_current(self.native, native)
                sent = True
                wire_request=httpx.Request('POST',base_url+CARRIER,content=wire,headers={'content-type':MEDIA},extensions=logical.extensions)
                async with _astream(self.http_client,wire_request) as response:
                    if response.status_code != 200 or response.headers.get('content-type', '').split(';')[0].strip().lower() != MEDIA or response.headers.get('content-encoding'):
                        raise TransportFailure('outcome_unknown')
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > 2 * response_limit:
                            raise TransportFailure('outcome_unknown')
                        body.extend(chunk)
                verified = json.loads(await self.jobs.run(ticket.complete, bytes(body), int(self.clock())))
                return httpx.Response(verified['status'], content=_bytes(verified['body_b64']), request=logical,
                    headers=[(h['name'].encode('ascii'), _bytes(h['value_b64'])) for h in verified['headers']])
        except asyncio.CancelledError:
            raise
        except TransportFailure:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise TransportFailure('outcome_unknown' if sent else 'not_sent', reason='timeout') from None
        except Exception:
            raise TransportFailure('outcome_unknown' if sent else 'not_sent') from None


@dataclass(frozen=True)
class TrustedPeer:
    connection_id: str
    node_id: str
    app_instance_id: str


class Middleware:
    def __init__(self, app, native, replay, *, trusted_ingress=None, jobs=None, clock=time.time, handler_timeout=30, rejection_timeout=2,
                 stream_timeout=3600, stream_idle_timeout=60):
        if type(handler_timeout) not in (int, float) or not 0 < handler_timeout <= 3600:
            raise ValueError('invalid_handler_timeout')
        if type(rejection_timeout) not in (int,float) or not 0<rejection_timeout<=3600:raise ValueError("invalid_rejection_timeout")
        self.rejection_timeout=rejection_timeout
        self.handler_timeout = handler_timeout
        for value in (stream_timeout, stream_idle_timeout):
            if type(value) not in (int, float) or not 0 < value <= 3600: raise ValueError('invalid_stream_timeout')
        self.stream_timeout, self.stream_idle_timeout = stream_timeout, stream_idle_timeout
        self._streams = 0
        self.app, self.native, self.replay = app, native, replay
        self.trusted_ingress = trusted_ingress
        self.jobs = jobs or NativeJobs()
        self.clock = clock

    async def __call__(self, scope, receive, send, *, request_budget=None):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        root_path = scope.get('root_path', '').rstrip('/')
        method, path = scope['method'], scope['path']
        if root_path and path.startswith(root_path + '/'):
            path = path[len(root_path):]
        elif root_path and path == root_path:
            path = '/'
        carrier = path == CARRIER
        if not carrier and not any(m==method and matches(p,path) for m,p in self.native.receiver_routes()):
            return await self.app(scope, receive, send)
        started = False
        outer_send = send
        async def tracked_send(message):
            nonlocal started
            if message['type'] == 'http.response.start': started = True
            await outer_send(message)
        send = tracked_send
        try:
            native = _acquire(self.native)
            peer = self.trusted_ingress(scope) if self.trusted_ingress is not None else None
            if not isinstance(peer, TrustedPeer):
                raise ValueError('untrusted_ingress')
            _admit_inbound(self.native, native, peer.connection_id)
            headers = scope.get('headers', [])
            types = [v for k, v in headers if k == b'content-type']
            if any(k == b'content-encoding' for k,v in headers):
                raise ValueError('unsupported_encoding')
            if carrier and (method != 'POST' or types != [MEDIA.encode()] or scope.get('query_string')):
                raise ValueError('invalid_carrier')
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] != 'http.request':
                    raise ValueError('disconnected')
                chunk = message.get('body', b'')
                if len(body) + len(chunk) > (MAX_WIRE if carrier else MAX_BODY):
                    raise ValueError('oversized')
                body.extend(chunk)
                if not message.get('more_body', False):
                    break
            if carrier:
                if hasattr(self.native, 'acquire_inbound'):
                    native = await self.jobs.run(self.native.acquire_inbound, bytes(body))
                ticket = await self.jobs.run(native.receive, bytes(body), int(self.clock()))
                await self.jobs.run(ticket.admit_transport, peer.connection_id, peer.node_id, peer.app_instance_id)
                _admit_inbound(self.native, native, peer.connection_id)
                digest, expiry = ticket.replay()
                await self.replay.reserve(digest, expiry, int(self.clock()))
                payload = json.loads(ticket.payload())
            else:
                metadata = _plain_metadata(headers)
                if hasattr(self.native, 'acquire_plain'):
                    native = self.native.acquire_plain(peer.connection_id, metadata)
                payload = _payload(method, path, scope.get('query_string', b''), _without_policy(headers), bytes(body))
                endpoint = await self.jobs.run(native.admit_plain, peer.connection_id, peer.node_id, peer.app_instance_id, metadata, _json(payload))
                ticket = None
            inner = dict(scope)
            inner['method'] = payload['method']
            inner['path'] = root_path + payload['path']
            inner['raw_path'] = inner['path'].encode('ascii')
            inner['query_string'] = _bytes(payload['query_b64'])
            content = _bytes(payload['body_b64'])
            inner['headers'] = [(h['name'].encode('ascii'), _bytes(h['value_b64'])) for h in payload['headers']]
            inner['headers'].append((b'content-length', str(len(content)).encode()))
            # This context is created only after admission, never from a header.
            inner['app_security.admitted'] = ticket if carrier else peer
            if not carrier and native.response_stream(endpoint):
                from .streaming import MAX_STREAMS, serve_stream
                if self._streams >= MAX_STREAMS: raise ValueError('stream_capacity')
                self._streams += 1
                try:
                    _admit_inbound(self.native, native, peer.connection_id)
                    # Only a native-admitted OFF stream declaration may replace
                    # the finite request budget, never a caller-controlled flag.
                    if request_budget is not None:
                        request_budget.reschedule(asyncio.get_running_loop().time() + self.stream_timeout)
                    return await serve_stream(self, native, endpoint, metadata, payload, inner,
                                              receive, send, self.stream_timeout,
                                              connection_id=peer.connection_id,
                                              opening_timeout=self.handler_timeout, idle_timeout=self.stream_idle_timeout)
                finally: self._streams -= 1
            response_limit = ticket.max_response_bytes() if carrier else native.max_response_bytes(endpoint)
            delivered = False
            async def inner_receive():
                nonlocal delivered
                if delivered:
                    return {'type': 'http.disconnect'}
                delivered = True
                return {'type': 'http.request', 'body': content, 'more_body': False}
            status, response_headers, response_body, finished = None, [], bytearray(), False
            async def capture(message):
                nonlocal status, response_headers, finished
                if message['type'] == 'http.response.start':
                    if status is not None:
                        raise ValueError('double_response')
                    status, response_headers = message['status'], message.get('headers', [])
                elif message['type'] == 'http.response.body':
                    if status is None or finished or message.get('more_body', False):
                        raise ValueError('unsupported_streaming')
                    part = message.get('body', b'')
                    if len(response_body) + len(part) > response_limit:
                        # Produce a bounded authenticated failure after admission.
                        # Discard all application headers/body; expose numeric sizes only.
                        measured = len(response_body) + len(part)
                        status = 502
                        response_headers = [(b'content-type', b'application/json')]
                        response_body.clear()
                        response_body.extend(_json({'error': {'code': 'response_too_large',
                            'response_bytes': measured, 'limit_bytes': response_limit}}))
                        finished = True
                        return
                    response_body.extend(part)
                    finished = True
                else:
                    raise ValueError('unsupported_response')
            _admit_inbound(self.native, native, peer.connection_id)
            if carrier and int(self.clock()) >= expiry:
                raise ValueError('request_expired')
            async with asyncio.timeout(self.handler_timeout):
                await self.app(inner, inner_receive, capture)
            if status is None or not finished:
                raise ValueError('incomplete_response')
            if carrier:
                result = _payload(payload['method'], payload['path'], _bytes(payload['query_b64']), response_headers, bytes(response_body), status, response_limit=response_limit)
                output = await self.jobs.run(ticket.respond, _json(result), int(self.clock()))
                await send({'type':'http.response.start', 'status':200, 'headers':[(b'content-type', MEDIA.encode())]})
                await send({'type':'http.response.body', 'body':output})
            else:
                # A handler cannot impersonate shell metadata or bypass its bounds.
                result = _payload(payload['method'], payload['path'], _bytes(payload['query_b64']), response_headers, bytes(response_body), status, response_limit=response_limit)
                # Same payload rules as ON, including forbidden headers/media.
                await self.jobs.run(native.validate_response_payload, endpoint, _json(result))
                response_headers = list(response_headers) + [(POLICY_HEADER, metadata.encode('ascii'))]
                await send({'type':'http.response.start', 'status':status, 'headers':response_headers})
                await send({'type':'http.response.body', 'body':bytes(response_body)})
        except asyncio.CancelledError:
            raise
        except Exception:
            if started: raise
            try:
                async with asyncio.timeout(self.rejection_timeout):
                    await send({'type':'http.response.start', 'status':503, 'headers':[(b'content-type', b'application/json')]})
                    await send({'type':'http.response.body', 'body':b'' if method=='HEAD' else b'{"error":"app_security_rejected"}'})
            except TimeoutError:pass


class SyncClient:
    """Synchronous HTTP boundary for existing thread-offloaded application callers."""
    def __init__(self, native, http_client, *, jobs, clock=time.time, trusted_egress=None):
        self.native, self.http_client, self.jobs, self.clock = native, http_client, jobs, clock
        self.trusted_egress = trusted_egress

    def post_json(self, *, connection, base_url, path, json_body, headers=None, timeout=15):
        try:body,headers=_json_input(json_body,headers)
        except Exception:raise TransportFailure('not_sent') from None
        return self.request(connection=connection,base_url=base_url,method='POST',path=path,body=body,headers=headers,timeout=timeout)

    def request(self, *, connection, base_url, method, path, query=b'', body=b'', headers=None, timeout=15):
        if type(timeout) not in (int,float) or not 0 < timeout <= 3600:
            raise TransportFailure('not_sent')
        end = time.monotonic() + timeout
        def remaining():
            budget = end - time.monotonic()
            if budget <= 0:
                raise TimeoutError
            return budget
        sent = False
        try:
            native = _acquire(self.native)
            endpoint, encrypted = native.route(connection,method,path)
            if native.response_stream(endpoint): raise ValueError('use_stream_api')
            response_limit=native.max_response_bytes(endpoint)
            logical=_logical(method,base_url,path,query,body,headers,remaining())
            base_url=normalize_base(base_url)
            payload=_payload(method,path,logical.url.query,logical.headers.raw,logical.content)
            _admit_egress(self.trusted_egress, native, connection, base_url, self.http_client)
            _admit_current(self.native, native)
            if not encrypted:
                self.jobs.run_sync(native.validate_outbound_plain,connection,endpoint,_json(payload),timeout=remaining())
                metadata = native.plain_policy(connection, endpoint)
                _admit_current(self.native, native)
                wire_headers = list(logical.headers.raw) + [(POLICY_HEADER, metadata.encode('ascii'))]
                sent=True
                wire_request=httpx.Request(method,logical.url,content=logical.content,headers=wire_headers,extensions={'timeout':httpx.Timeout(remaining()).as_dict()})
                with _stream(self.http_client,wire_request) as response:
                    if response.headers.get('content-encoding'):
                        raise TransportFailure('outcome_unknown')
                    response_metadata = _plain_metadata(response.headers.raw)
                    if response_metadata != metadata:
                        raise TransportFailure('outcome_unknown')
                    response_headers = _plain_response_headers(response.headers.raw)
                    body=bytearray()
                    for chunk in response.iter_raw():
                        remaining()
                        if len(body)+len(chunk)>response_limit:
                            raise TransportFailure('outcome_unknown')
                        body.extend(chunk)
                    remaining()
                    result = _payload(method, path, logical.url.query, response_headers, bytes(body), response.status_code, response_limit=response_limit)
                    self.jobs.run_sync(native.validate_plain_response, connection, endpoint, response_metadata, _json(result), timeout=remaining())
                    return httpx.Response(response.status_code,content=bytes(body),headers=response_headers,request=logical)
            ticket=self.jobs.run_sync(native.begin,connection,endpoint,_json(payload),int(self.clock()),timeout=remaining())
            _admit_current(self.native, native)
            sent=True
            wire_request=httpx.Request('POST',base_url+CARRIER,content=ticket.wire(),headers={'content-type':MEDIA},extensions={'timeout':httpx.Timeout(remaining()).as_dict()})
            with _stream(self.http_client,wire_request) as response:
                if response.status_code!=200 or response.headers.get('content-type','').split(';')[0].strip().lower()!=MEDIA or response.headers.get('content-encoding'):
                    raise TransportFailure('outcome_unknown')
                body=bytearray()
                for chunk in response.iter_raw():
                    remaining()
                    if len(body)+len(chunk)>2*response_limit:
                        raise TransportFailure('outcome_unknown')
                    body.extend(chunk)
            verified=json.loads(self.jobs.run_sync(ticket.complete,bytes(body),int(self.clock()),timeout=remaining()))
            return httpx.Response(verified['status'],content=_bytes(verified['body_b64']),request=logical,
                headers=[(h['name'].encode('ascii'),_bytes(h['value_b64'])) for h in verified['headers']])
        except TransportFailure:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise TransportFailure('outcome_unknown' if sent else 'not_sent', reason='timeout') from None
        except Exception:
            raise TransportFailure('outcome_unknown' if sent else 'not_sent') from None
