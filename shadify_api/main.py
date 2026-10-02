from contextlib import asynccontextmanager
from typing import Callable
from uuid import UUID
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from .media import MediaError, MediaService, UploadIntent
from .domain import TrackDraft
from .storage import StorageUnavailable


def require_identity():
    # Deliberately closed until the existing Authentication trust path is connected.
    # Never accept account IDs or unverified forwarded headers from a browser.
    raise HTTPException(503, "Authentication integration pending")


def create_app(service: MediaService | None = None, identity: Callable = require_identity,
               *, domain=None, repository=None, binding=None, readiness=None):
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if binding is not None:
                await binding.close()

    app = FastAPI(title="Shadify API", version="0.2.0", lifespan=lifespan)
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
    def ready():
        if readiness is None:
            return JSONResponse(status_code=503, content={"status": "pending_integration"})
        try:
            available, checks = readiness()
        except Exception:
            available, checks = False, {"runtime": "unavailable"}
        return JSONResponse(status_code=200 if available else 503,
                            content={"status": "ready" if available else "pending_integration", "checks": checks})

    def domain_service():
        if domain is None:
            raise HTTPException(503, "Domain persistence integration pending")
        return domain

    @app.get("/api/me")
    def me(owner: str = Depends(identity), data=Depends(domain_service)):
        return data.me(owner)

    @app.post("/api/artists/{artist_id}/tracks", status_code=201)
    def create_track(artist_id: UUID, draft: TrackDraft, owner: str = Depends(identity),
                     data=Depends(domain_service)):
        return data.create_track(owner, artist_id, draft)

    @app.get("/api/artists/{artist_id}/tracks")
    def tracks(artist_id: UUID, owner: str = Depends(identity), data=Depends(domain_service)):
        return data.tracks(owner, artist_id)

    @app.get("/api/media/{asset_id}")
    def media_metadata(asset_id: UUID, owner: str = Depends(identity)):
        if repository is None:
            raise HTTPException(503, "Media persistence integration pending")
        asset = repository.get(asset_id)
        if (asset is None or asset.owner_id != owner
                or not repository.owns_track(owner, asset.artist_id, asset.track_id)):
            raise HTTPException(404, "Asset not found")
        return {"asset_id": str(asset.id), "artist_id": str(asset.artist_id), "track_id": str(asset.track_id),
                "asset_type": asset.asset_type, "mime": asset.mime, "size": asset.size,
                "state": asset.state, "access_class": asset.access_class, "publish_state": asset.publish_state}

    @app.post("/api/media/uploads", status_code=201)
    def create_upload(intent: UploadIntent, owner: str = Depends(identity),
                      media: MediaService = Depends(media_service)):
        asset, authorization = media.create(owner, intent)
        return {"asset_id": str(asset.id), "state": asset.state,
                "upload": {"method": "PUT", "url": authorization.url,
                "headers": authorization.headers, "expires_in": authorization.expires_in}}

    @app.post("/api/media/uploads/{asset_id}/complete")
    def complete_upload(asset_id: UUID, owner: str = Depends(identity),
                        media: MediaService = Depends(media_service)):
        asset = media.complete(owner, asset_id)
        return {"asset_id": str(asset.id), "state": asset.state,
                "access_class": asset.access_class, "publish_state": asset.publish_state}

    return app


app = create_app()
