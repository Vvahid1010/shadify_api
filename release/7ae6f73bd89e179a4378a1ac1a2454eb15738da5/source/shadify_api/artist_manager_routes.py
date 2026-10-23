"""Explicit optional artist-manager routes; identity/ingress remain owner-wired."""
from uuid import UUID
from typing import Annotated
from fastapi import Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from .auth import Identity


class ManagerRevision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=0, le=9223372036854775807)


class ManagerPermissions(ManagerRevision):
    permissions: list[str]


def install_artist_manager_routes(app, identity, repository):
    def repo():
        if repository is None:
            raise HTTPException(503, 'Artist persistence integration pending')
        return repository

    @app.get('/api/admin/artists/{artist_id}/managers')
    def managers(artist_id: UUID, actor: Identity = Depends(identity), data=Depends(repo)):
        return data.artist_managers(actor, artist_id)

    @app.put('/api/admin/artists/{artist_id}/managers/{account_id}')
    def grant(artist_id: UUID, account_id: Annotated[str, Path(min_length=1, max_length=36)], body: ManagerPermissions,
              actor: Identity = Depends(identity), data=Depends(repo)):
        return data.set_artist_manager(actor, artist_id, account_id, body.permissions,
                                       expected_revision=body.expected_revision)

    @app.delete('/api/admin/artists/{artist_id}/managers/{account_id}')
    def revoke(artist_id: UUID, account_id: Annotated[str, Path(min_length=1, max_length=36)], body: ManagerRevision,
               actor: Identity = Depends(identity), data=Depends(repo)):
        return data.set_artist_manager(actor, artist_id, account_id, [],
                                       expected_revision=body.expected_revision)
