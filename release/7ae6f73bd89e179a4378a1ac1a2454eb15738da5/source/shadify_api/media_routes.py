"""Dedicated capability-only byte listener. Never mount around GuardedASGI.

Node owner routes only /media/{UUID} to this listener, without query/access logs.
Business and issuance routes remain on the existing canonical guarded receiver.
"""
import os
import base64
import hashlib
import hmac
import re
import time
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from uuid import UUID
from .auth import require_fresh_actor
from .local_storage import LocalAdapter
from .media import MediaError
from .storage import StorageUnavailable, StorageAuthorizationExpired

# Per-response clock only; no shared permission cache. Check before the next
# chunk after two seconds, including after a queued read before yielding it.
# Initial/post-wait authorization always precedes headers and the first byte.
AUTHORITY_RECHECK_SECONDS = 2.0


def byte_range(header, size):
    if header is None: return 0, size - 1, 200
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not any(match.groups()): raise MediaError(416, "Range unavailable")
    first, last = match.groups()
    if not first:
        if int(last) <= 0: raise MediaError(416, "Range unavailable")
        start, end = max(0, size - int(last)), size - 1
    else:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    if start > end or start >= size: raise MediaError(416, "Range unavailable")
    return start, end, 206


def create_media_app(storage):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def private_response(request, call_next):
        try: response = await call_next(request)
        except Exception: response = JSONResponse({'detail': 'Media operation unavailable'}, status_code=503)
        response.headers.update({'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer',
                                 'X-Content-Type-Options': 'nosniff'})
        return response

    @app.exception_handler(MediaError)
    async def error(request, error):
        return JSONResponse({'detail': error.detail}, status_code=error.status)

    @app.exception_handler(StorageAuthorizationExpired)
    async def expired(request, error):
        return JSONResponse({'detail': 'Media authorization expired'}, status_code=403)

    @app.exception_handler(StorageUnavailable)
    async def unavailable(request, error):
        return JSONResponse({'detail': 'Storage operation unavailable'}, status_code=503)

    @app.api_route('/media/{asset_id}', methods=['PUT', 'GET', 'HEAD'])
    async def bytes_route(asset_id: UUID, request: Request):
        tokens = request.query_params.getlist('token')
        if len(tokens) != 1 or set(request.query_params) != {'token'}:
            raise MediaError(403, 'Media authorization unavailable')
        claims, actor, asset = await run_in_threadpool(storage.validate, tokens[0], asset_id, request.method)
        if claims['exp'] <= int(time.time()): raise MediaError(403, 'Media authorization expired')
        adapter = storage.adapter(asset)
        if request.method == 'PUT':
            if (request.headers.get('content-type') != asset.mime or request.headers.get('if-none-match') != '*'
                    or request.headers.get('content-encoding') not in (None, 'identity')):
                raise MediaError(422, 'Upload headers mismatch')
            length = request.headers.get('content-length')
            if length is not None and length != str(asset.size): raise MediaError(422, 'Upload size mismatch')
            checksum = request.headers.get('x-amz-checksum-sha256')
            if checksum is not None:
                try:
                    decoded = base64.b64decode(checksum, validate=True)
                    if len(decoded) != 32 or base64.b64encode(decoded).decode() != checksum: raise ValueError()
                except ValueError: raise MediaError(422, 'Invalid upload checksum') from None
            await run_in_threadpool(storage.repository.check_upload, actor, asset)
            if not isinstance(adapter, LocalAdapter):
                # A bounded R2 authorization is created only after current app
                # checks; 307 preserves PUT and its immutable headers.
                url = await run_in_threadpool(adapter.authorize_upload, asset.key, asset.mime, asset.size,
                                              deadline=claims['exp'], checksum=checksum)
                return RedirectResponse(url.url, status_code=307)
            installed = False
            try:
                with adapter.stage(asset.key, asset.mime) as staged:
                    total = 0
                    digest = hashlib.sha256()
                    async for chunk in request.stream():
                        if claims['exp'] <= int(time.time()): raise MediaError(403, 'Media authorization expired')
                        total += len(chunk)
                        digest.update(chunk)
                        if total > asset.size: raise MediaError(413, 'Upload size exceeded')
                        view = memoryview(chunk)
                        while view:
                            written = await run_in_threadpool(os.write, staged[0], view)
                            if written <= 0: raise StorageUnavailable('Local upload unavailable')
                            view = view[written:]
                    if total != asset.size: raise MediaError(422, 'Upload truncated')
                    if checksum is not None and not hmac.compare_digest(checksum, base64.b64encode(digest.digest()).decode()):
                        raise MediaError(422, 'Upload checksum mismatch')
                    def commit():
                        nonlocal installed
                        # Locks current artist/admin/actor/asset during final file
                        # publication, never during browser upload streaming.
                        with storage.repository.upload_guard(actor, asset):
                            if claims['exp'] <= int(time.time()): raise MediaError(403, 'Media authorization expired')
                            require_fresh_actor(actor)
                            adapter.install(staged); installed = True
                            require_fresh_actor(actor)
                    try: await run_in_threadpool(commit)
                    except BaseException:
                        if installed: adapter.remove_installed(staged)
                        raise
            except FileExistsError: raise MediaError(412, 'Object already exists') from None
            except OSError: raise StorageUnavailable('Local upload unavailable') from None
            return Response(status_code=201)

        await run_in_threadpool(storage.authorize_read, claims, actor, asset)
        if claims['exp'] <= int(time.time()): raise MediaError(403, 'Media authorization expired')
        if not isinstance(adapter, LocalAdapter):
            # Do not extend the app capability by a fresh full provider TTL.
            access = await run_in_threadpool(adapter.create_playback_access, asset.key,
                                            deadline=claims['exp'], method=request.method)
            return RedirectResponse(access.url, status_code=307)
        fd = await run_in_threadpool(adapter.open, asset.key)
        try:
            if claims['exp'] <= int(time.time()): raise MediaError(403, 'Media authorization expired')
            identity = await run_in_threadpool(storage.require_file, asset, fd)
            if claims['exp'] <= int(time.time()): raise MediaError(403, 'Media authorization expired')
            if actor is not None: require_fresh_actor(actor)
            # Opening/verifying a file may wait. Recheck current rights/source
            # after those waits, before HEAD headers or any GET/Range body.
            await run_in_threadpool(storage.authorize_read,claims,actor,asset)
            if claims['exp']<=int(time.time()):raise MediaError(403,'Media authorization expired')
            size = os.fstat(fd).st_size
            if size != asset.size: raise StorageUnavailable('Object size changed')
            try: start, end, status = byte_range(request.headers.get('range'), size)
            except MediaError:
                return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
            headers = {'Content-Length': str(end - start + 1), 'Content-Type': asset.mime, 'Accept-Ranges': 'bytes'}
            if status == 206: headers['Content-Range'] = f'bytes {start}-{end}/{size}'
            if request.method == 'HEAD': return Response(status_code=status, headers=headers)
            os.lseek(fd, start, os.SEEK_SET)
            stream_fd = fd
            last_authority_check = time.monotonic()
            async def chunks():
                nonlocal last_authority_check
                try:
                    remaining = end - start + 1
                    first=True
                    while remaining:
                        # TTL and authority are rechecked during delivery too.
                        if claims['exp'] <= int(time.time()): return
                        now = time.monotonic()
                        if first or now - last_authority_check >= AUTHORITY_RECHECK_SECONDS:
                            await run_in_threadpool(storage.authorize_read, claims, actor, asset)
                            last_authority_check = time.monotonic();first=False
                            if claims['exp'] <= int(time.time()): return
                        info=adapter.validate_descriptor(stream_fd,asset.key)
                        if dict(device=info.st_dev,inode=info.st_ino,size=info.st_size,mtime_ns=info.st_mtime_ns,ctime_ns=info.st_ctime_ns)!=identity: return
                        block = await run_in_threadpool(os.read, stream_fd, min(65536, remaining))
                        # A queued filesystem read must not extend the grant.
                        if claims['exp'] <= int(time.time()) or not block: return
                        # A slow/queued read can cross the bounded authority
                        # interval. Recheck BEFORE yielding its returned bytes.
                        if time.monotonic()-last_authority_check>=AUTHORITY_RECHECK_SECONDS:
                            await run_in_threadpool(storage.authorize_read,claims,actor,asset)
                            last_authority_check=time.monotonic()
                            if claims['exp']<=int(time.time()):return
                        info=adapter.validate_descriptor(stream_fd,asset.key)
                        if dict(device=info.st_dev,inode=info.st_ino,size=info.st_size,mtime_ns=info.st_mtime_ns,ctime_ns=info.st_ctime_ns)!=identity: return
                        remaining -= len(block)
                        yield block
                except (MediaError, StorageUnavailable):
                    return  # Stop a revoked/expired response; declared length prevents false complete success.
                finally: os.close(stream_fd)
            response = StreamingResponse(chunks(), status_code=status, headers=headers)
            fd = None
            return response
        finally:
            if fd is not None: os.close(fd)

    @app.get('/media/v1/tracks/{track_id}/access')
    async def guest_access(track_id: UUID, request: Request):
        # Public product authorization only; no synthetic Authentication identity.
        # This cannot issue private originals or unentitled paid full access.
        params=request.query_params
        if set(params)-{'preview','quality','save_data'} or any(len(params.getlist(k))!=1 for k in params):raise MediaError(422,'Invalid playback query')
        preview=params.get('preview','false');quality=params.get('quality');hint=params.get('save_data')
        if preview not in ('true','false') or quality is not None and quality not in ('auto','high','data_saver') or hint is not None and (quality!='auto' or hint not in ('true','false')) or preview=='true' and quality is not None:raise MediaError(422,'Invalid playback quality')
        if quality is None:
            access=await run_in_threadpool(storage.playback_access,track_id,preview=preview=='true')
            return {'url':access.url,'expires_at':access.expires_at.isoformat()}
        selection=await run_in_threadpool(storage.select_quality,track_id,quality,hint=='true')
        access=await run_in_threadpool(storage.issue_quality,selection)
        # This is an actual post-await source/rights guard, not just validation
        # inside the preceding potentially queued issuance callback.
        await run_in_threadpool(storage.authorize_quality,selection)
        if access.expires_at.timestamp()<=time.time():raise MediaError(403,'Media authorization expired')
        return {'url':access.url,'expires_at':access.expires_at.isoformat(),'selected_quality':selection['selected_quality'],'fallback':selection['fallback']}

    return app
