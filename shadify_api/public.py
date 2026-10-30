"""Explicit public aliases inside the existing protected Shadify API.

Mount only under the existing GuardedASGI application. No listener or runtime is
created; repository/storage callbacks retain public visibility and media gates.
"""
import json
import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response

PREFIX = "/api/public"
MAX_RESPONSE = 8 * 1024 * 1024


def create_public_router(*, repository=None, storage=None, editorial=None):
    app = APIRouter()

    def output(value):
        raw = json.dumps(jsonable_encoder(value), ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > MAX_RESPONSE:
            raise HTTPException(503, "Public response unavailable")
        return Response(raw, media_type="application/json", headers={"Cache-Control":"no-store", "Referrer-Policy":"no-referrer", "X-Content-Type-Options":"nosniff"})

    def available(value):
        if value is None:
            raise HTTPException(503, "Public integration unavailable")
        return value

    def inputs(allowed):
        async def validate(request: Request):
            if request.scope.get("raw_path") != request.url.path.encode("ascii"):
                raise HTTPException(422, "Noncanonical public path")
            if any(k not in allowed or len(request.query_params.getlist(k)) != 1
                   for k in request.query_params):
                raise HTTPException(422, "Invalid public query")
            async for chunk in request.stream():
                if chunk:
                    raise HTTPException(422, "Public reads accept no body")
        return validate

    @app.get(PREFIX + "/artists", dependencies=[Depends(inputs({"limit", "cursor", "q"}))])
    def artists(limit: int = Query(20, ge=1, le=20), cursor: UUID | None = None,
                q: str = Query("", max_length=120)):
        return output(available(repository).public_artist_catalog(limit, cursor, q.strip()))

    @app.get(PREFIX + "/artists/{artist}", dependencies=[Depends(inputs({"limit", "cursor"}))])
    def artist(artist: str, limit: int = Query(20, ge=1, le=20), cursor: UUID | None = None):
        try:
            selected = {"artist_id": UUID(artist)}
        except ValueError:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]", artist):
                raise HTTPException(422, "Invalid artist identifier") from None
            selected = {"slug": artist}
        return output(available(repository).public_page(**selected, limit=limit, cursor=cursor))

    @app.get(PREFIX + "/artists/{artist_id}/images/{purpose}",
             dependencies=[Depends(inputs({"format", "size"}))])
    def image(artist_id: UUID, purpose: Literal["avatar", "header"],
              format: Literal["png", "avif"] = "png", size: Literal["full", "small"] = "full"):
        data = available(repository)
        anchor = data.public_profile_asset(artist_id, purpose)
        asset = data.public_profile_asset(artist_id, purpose, format=format, size=size)
        access = available(storage() if callable(storage) else storage).profile_access(artist_id, purpose, expected_asset=asset.id)
        return output(dict(url=access.url, expires_at=access.expires_at, artist_id=str(artist_id),
                           asset_id=str(asset.id), anchor_asset_id=str(anchor.id), purpose=purpose,
                           mime=asset.mime, size=asset.size))

    def cover(resource, content_id):
        asset = available(repository).public_cover_asset(resource, content_id)
        access = available(storage() if callable(storage) else storage).cover_access(resource, content_id, expected_asset=asset.id)
        return output(dict(url=access.url, expires_at=access.expires_at,
                           artist_id=str(asset.artist_id), resource=resource, content_id=str(content_id),
                           asset_id=str(asset.id), mime=asset.mime, size=asset.size))

    @app.get(PREFIX + "/releases/{content_id}/cover", dependencies=[Depends(inputs(set()))])
    def release_cover(content_id: UUID):
        return cover("releases", content_id)

    @app.get(PREFIX + "/tracks/{content_id}/cover", dependencies=[Depends(inputs(set()))])
    def track_cover(content_id: UUID):
        return cover("tracks", content_id)

    @app.get(PREFIX + "/discover/editorial", dependencies=[Depends(inputs(set()))])
    def discovery():
        return output(available(editorial).public())

    return app


def public_receiver_catalog():
    routes = (("artists", "/artists"), ("artist", "/artists/{artist}"),
              ("image", "/artists/{artist_id}/images/{purpose}"),
              ("release_cover", "/releases/{content_id}/cover"),
              ("track_cover", "/tracks/{content_id}/cover"),
              ("editorial", "/discover/editorial"))
    return [dict(endpoint_id="shadify_api.public." + name, receiver_app="shadify_api",
                 permitted_peer_apps=["authentication"], method="GET", path=PREFIX + path,
                 headers=["accept", "cache-control", "content-type", "referrer-policy", "x-content-type-options"], off_supported=True,
                 proxy_headers=False, max_response_bytes=MAX_RESPONSE)
            for name, path in routes]
