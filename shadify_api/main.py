from contextlib import asynccontextmanager
import inspect
from typing import Callable
from uuid import UUID
from fastapi import Depends, FastAPI, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from .media import MediaError, MediaService, UploadIntent
from .domain import TrackDraft
from .storage import StorageUnavailable
from .auth import Identity


def require_identity():
    # Deliberately closed until the existing Authentication trust path is connected.
    # Never accept account IDs or unverified forwarded headers from a browser.
    raise HTTPException(503, "Authentication integration pending")


def create_app(service: MediaService | None = None, identity: Callable = require_identity,
               *, domain=None, repository=None, binding=None, readiness=None, receiver=None, nomination=None):
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
        response.headers["Cache-Control"] = "no-store"
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
                "access_class": asset.access_class, "publish_state": asset.publish_state}

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

    @app.post('/api/tracks/{track_id}/playback')
    def playback_access(track_id: UUID, preview: bool = False, owner: Identity = Depends(identity)):
        # No entitlement inferred from this identity. Canonical guest issuance
        # ingress remains Node/Auth-owned; this route does not bypass admission.
        access = common_storage().playback_access(track_id, preview=preview)
        return {'url': access.url, 'expires_at': access.expires_at}

    from .nomination_routes import install_nomination_routes
    install_nomination_routes(app,identity,nomination)

    from .artist_routes import install_artist_routes
    install_artist_routes(app, identity, repository)
    return app


app = create_app()
