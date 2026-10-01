from typing import Callable
from uuid import UUID
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from .media import MediaError, MediaService, UploadIntent
from .storage import StorageUnavailable


def require_identity():
    # Deliberately closed until the existing Authentication trust path is connected.
    # Never accept account IDs or unverified forwarded headers from a browser.
    raise HTTPException(503, "Authentication integration pending")


def create_app(service: MediaService | None = None, identity: Callable = require_identity):
    app = FastAPI(title="Shadify API", version="0.1.0")

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
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
        # Wiring is not proof of a live database/R2/Authentication connection.
        return JSONResponse(status_code=503, content={"status": "pending_integration"})

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
