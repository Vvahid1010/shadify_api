from contextlib import asynccontextmanager
import inspect
from typing import Callable
from typing import Literal
from uuid import UUID
from fastapi import Depends, FastAPI, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from .media import MediaError, MediaService, UploadIntent
from .domain import TrackDraft
from .storage import StorageUnavailable
from .auth import Identity
from .profile_media import ProfileUploadIntent


def require_identity():
    # Deliberately closed until the existing Authentication trust path is connected.
    # Never accept account IDs or unverified forwarded headers from a browser.
    raise HTTPException(503, "Authentication integration pending")


def create_app(service: MediaService | None = None, identity: Callable = require_identity,
               *, domain=None, repository=None, binding=None, readiness=None, receiver=None, nomination=None, musicbrainz_provider=None):
    @asynccontextmanager
    async def lifespan(app):
        try:
            if binding is not None:
                try:
                    await binding.ready()
                except Exception:
                    pass  # Liveness survives missing/invalid projection; admission stays closed.
            yield
        finally:
            if binding is not None:
                try:
                    await binding.close()
                finally:
                    if receiver is not None:
                        receiver.close()

    app = FastAPI(title="Shadify API", version="0.2.0", lifespan=lifespan)
    app.state.managed_receiver = receiver
    if binding is not None:
        from app_security_shell.receiver import GuardedASGI
        app.state.app_security = binding
        app.add_middleware(GuardedASGI, binding=binding)

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            # Provider/database exception strings may contain protected runtime data.
            response = JSONResponse(status_code=503, content={"detail": "API operation unavailable"})
        media_access = (request.method == 'POST' and
                        (request.url.path.startswith('/api/media/') and request.url.path.endswith('/access')
                         or request.url.path.startswith('/api/tracks/') and request.url.path.endswith('/playback')))
        response.headers["Cache-Control"] = "private, no-store" if media_access else "no-store"
        return response

    @app.exception_handler(MediaError)
    async def media_error(request, error):
        return JSONResponse(status_code=error.status, content={"detail": error.detail})

    @app.exception_handler(StorageUnavailable)
    async def storage_error(request, error):
        return JSONResponse(status_code=503, content={"detail": "Storage operation unavailable"})

    def media_service():
        if service is None:
            raise HTTPException(503, "Media persistence/storage integration pending")
        return service

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "shadify-api", "mode": "foundation"}

    @app.get("/health/ready")
    async def ready():
        if readiness is None:
            return JSONResponse(status_code=503, content={"status": "pending_integration"})
        try:
            result = readiness()
            available, checks = await result if inspect.isawaitable(result) else result
        except Exception:
            available, checks = False, {"runtime": "unavailable"}
        return JSONResponse(status_code=200 if available else 503,
                            content={"status": "ready" if available else "pending_integration", "checks": checks})

    def domain_service():
        if domain is None:
            raise HTTPException(503, "Domain persistence integration pending")
        return domain

    @app.get("/api/me")
    def me(request:Request,owner: Identity = Depends(identity), data=Depends(domain_service)):
        from .nomination_routes import finalize_me
        finalize_me(nomination,request,owner)
        return data.me(owner)

    @app.post("/api/artists/{artist_id}/tracks", status_code=201)
    def create_track(artist_id: UUID, draft: TrackDraft, owner: Identity = Depends(identity),
                     data=Depends(domain_service)):
        return data.create_track(owner, artist_id, draft)

    @app.get("/api/artists/{artist_id}/tracks")
    def tracks(artist_id: UUID, owner: Identity = Depends(identity), data=Depends(domain_service),
               limit: int = Query(20, ge=1, le=100), cursor: UUID | None = None):
        return data.tracks(owner, artist_id, limit, cursor)

    @app.get('/api/artists/{artist_id}/uploads')
    def studio_uploads(artist_id:UUID,owner:Identity=Depends(identity),track_id:UUID|None=None,
                       limit:int=Query(20,ge=1,le=20),cursor:UUID|None=None):
        if repository is None:raise HTTPException(503,'Media persistence integration pending')
        return repository.studio_uploads(owner,artist_id,track_id=track_id,limit=limit,cursor=cursor)

    @app.get("/api/media/{asset_id}")
    def media_metadata(asset_id: UUID, owner: Identity = Depends(identity)):
        if repository is None:
            raise HTTPException(503, "Media persistence integration pending")
        return repository.media_metadata(owner, asset_id)

    @app.post("/api/media/uploads", status_code=201)
    def create_upload(intent: UploadIntent, owner: Identity = Depends(identity),
                      media: MediaService = Depends(media_service)):
        asset, authorization = media.create(owner, intent)
        context = repository.access_context(owner, asset.artist_id) if repository is not None else {
            "actor_account_id": owner.account_id, "artist_id": str(asset.artist_id)}
        return {**context, "asset_id": str(asset.id), "state": asset.state,
                "upload": {"method": "PUT", "url": authorization.url,
                "headers": authorization.headers, "expires_in": authorization.expires_in}}

    @app.post("/api/media/uploads/{asset_id}/complete")
    def complete_upload(asset_id: UUID, owner: Identity = Depends(identity),
                        media: MediaService = Depends(media_service)):
        asset = media.complete(owner, asset_id)
        context = repository.access_context(owner, asset.artist_id) if repository is not None else {
            "actor_account_id": owner.account_id, "artist_id": str(asset.artist_id)}
        return {**context, "asset_id": str(asset.id), "state": asset.state,
                "access_class": asset.access_class, "publish_state": asset.publish_state,
                "ingest": repository.media_metadata(owner,asset_id)["ingest"] if repository is not None else None}

    @app.post('/api/artists/{artist_id}/profile-media/uploads',status_code=201)
    def create_profile_upload(artist_id:UUID,intent:ProfileUploadIntent,owner:Identity=Depends(identity),media:MediaService=Depends(media_service)):
        asset,authorization=media.create_profile(owner,artist_id,intent)
        context=repository.access_context(owner,artist_id)
        return {**context,'asset_id':str(asset.id),'state':asset.state,
                'upload':{'method':'PUT','url':authorization.url,'headers':authorization.headers,'expires_in':authorization.expires_in}}

    def common_storage():
        from .storage_service import StorageService
        media = media_service()
        if not isinstance(media.storage, StorageService):
            raise HTTPException(503, 'Media delivery integration pending')
        return media.storage

    @app.post('/api/media/{asset_id}/access')
    def private_media_access(asset_id: UUID, owner: Identity = Depends(identity)):
        access = common_storage().private_access(owner, asset_id)
        return {'url': access.url, 'expires_at': access.expires_at}

    @app.get('/api/artists/{artist_id}/profile-media/{purpose}/access')
    def public_profile_access(artist_id:UUID,purpose:Literal['avatar','header'],format:Literal['png','avif']='png',size:Literal['full','small']='full'):
        anchor=repository.public_profile_asset(artist_id,purpose)
        asset=repository.public_profile_asset(artist_id,purpose,format=format,size=size)
        access=common_storage().profile_access(artist_id,purpose,expected_asset=asset.id)
        return {'url':access.url,'expires_at':access.expires_at,'artist_id':str(artist_id),
                'asset_id':str(asset.id),'anchor_asset_id':str(anchor.id),'purpose':purpose,'mime':asset.mime,'size':asset.size}

    def public_cover(resource,content_id,request):
        if request.query_params or request.headers.get('content-length') not in (None,'0') or request.headers.get('transfer-encoding'):
            raise HTTPException(422,'Cover reads accept no query or body')
        asset=repository.public_cover_asset(resource,content_id)
        access=common_storage().cover_access(resource,content_id,expected_asset=asset.id)
        return {'url':access.url,'expires_at':access.expires_at,'artist_id':str(asset.artist_id),
                'resource':resource,'content_id':str(content_id),'asset_id':str(asset.id),'mime':asset.mime,'size':asset.size}

    @app.get('/api/releases/{release_id}/cover/access')
    def release_cover(release_id:UUID,request:Request):
        return public_cover('releases',release_id,request)

    @app.get('/api/tracks/{track_id}/cover/access')
    def track_cover(track_id:UUID,request:Request):
        return public_cover('tracks',track_id,request)

    @app.post('/api/tracks/{track_id}/playback')
    def playback_access(track_id: UUID, preview: bool = False, owner: Identity = Depends(identity)):
        # No entitlement inferred from this identity. Canonical guest issuance
        # ingress remains Node/Auth-owned; this route does not bypass admission.
        access = common_storage().playback_access(track_id, preview=preview)
        return {'url': access.url, 'expires_at': access.expires_at}

    from .ingest_review import MatchReview
    @app.get('/api/media/{asset_id}/match-review')
    def match_review_read(asset_id:UUID,owner:Identity=Depends(identity)):
        if repository is None:raise HTTPException(503,'Persistence unavailable')
        return repository.match_review(owner,asset_id)
    @app.put('/api/media/{asset_id}/match-review')
    def match_review_write(asset_id:UUID,body:MatchReview,owner:Identity=Depends(identity)):
        if repository is None:raise HTTPException(503,'Persistence unavailable')
        return repository.match_review(owner,asset_id,body)

    from .nomination_routes import install_nomination_routes
    install_nomination_routes(app,identity,nomination)

    from .artist_routes import install_artist_routes
    install_artist_routes(app, identity, repository)
    from .editorial_discovery_routes import install_editorial_discovery_routes
    install_editorial_discovery_routes(app, identity, repository)
    from .musicbrainz_search import install_musicbrainz_search_route
    install_musicbrainz_search_route(app, identity, repository, musicbrainz_provider)
    from .public import create_public_router
    from .editorial_discovery import EditorialDiscovery
    app.include_router(create_public_router(repository=repository, storage=common_storage,
                                            editorial=EditorialDiscovery(repository)))
    return app


app = create_app()
