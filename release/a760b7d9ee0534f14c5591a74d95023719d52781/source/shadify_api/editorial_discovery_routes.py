"""Explicit installer only; parent owns core route integration and managed enrollment."""
from uuid import UUID
from fastapi import Depends, HTTPException, Query
from .auth import Identity
from .editorial_discovery import EditorialDiscovery, DraftWrite, Revision, EditorRevision


def install_editorial_discovery_routes(app,identity,repository):
    def service():
        if repository is None:raise HTTPException(503,'Discover persistence unavailable')
        return EditorialDiscovery(repository)

    @app.get('/api/discover/editorial')
    def public(data=Depends(service)):
        return data.public()

    @app.get('/api/admin/discover')
    def read(actor:Identity=Depends(identity),data=Depends(service)):
        return data.read(actor)

    @app.patch('/api/admin/discover/draft')
    def save(body:DraftWrite,actor:Identity=Depends(identity),data=Depends(service)):
        return data.save(actor,body)

    @app.post('/api/admin/discover/submit')
    def submit(body:Revision,actor:Identity=Depends(identity),data=Depends(service)):
        return data.submit(actor,body)

    @app.post('/api/admin/discover/publish')
    def publish(body:Revision,actor:Identity=Depends(identity),data=Depends(service)):
        return data.publish(actor,body)

    @app.get('/api/admin/discover/candidates')
    def candidates(kind:str,q:str=Query('',max_length=120),mood:str|None=None,cursor:UUID|None=None,
                   actor:Identity=Depends(identity),data=Depends(service)):
        return data.candidates(actor,kind,q=q,mood=mood,cursor=cursor)

    @app.put('/api/admin/discover/editors/{account_id}')
    def grant(account_id:str,body:EditorRevision,actor:Identity=Depends(identity),data=Depends(service)):
        return data.editor(actor,account_id,body,grant=True)

    @app.delete('/api/admin/discover/editors/{account_id}')
    def revoke(account_id:str,body:EditorRevision,actor:Identity=Depends(identity),data=Depends(service)):
        return data.editor(actor,account_id,body,grant=False)
