"""Application-owned core routes; managed guest/proxy enrollment remains separate."""
from uuid import UUID
from fastapi import Body, Depends, HTTPException, Query
from .artist_models import (ArtistCreate, Layout, AssignOwner, TransferOwner, RevokeOwner,
                            Publication, Suspension, Verification, RESOURCE_MODELS)


def install_artist_routes(app, identity, repository):
    def repo():
        if repository is None: raise HTTPException(503,'Artist persistence integration pending')
        return repository

    @app.get('/api/me/artists')
    def manageable(actor:str=Depends(identity), data=Depends(repo),limit:int=Query(20,ge=1,le=100),cursor:UUID|None=None):
        return data.manageable_pages(actor,limit,cursor)

    @app.post('/api/admin/artists',status_code=201)
    def create(body:ArtistCreate,actor:str=Depends(identity),data=Depends(repo)):
        return data.create_artist(actor,body)

    @app.get('/api/artist-slugs/{slug}/public')
    def public_slug(slug:str,data=Depends(repo),limit:int=Query(20,ge=1,le=20),cursor:UUID|None=None):
        return data.public_page(slug=slug.lower(),limit=limit,cursor=cursor)

    @app.get('/api/artists/{artist_id}/public')
    def public(artist_id:UUID,data=Depends(repo),limit:int=Query(20,ge=1,le=20),cursor:UUID|None=None):
        return data.public_page(artist_id=artist_id,limit=limit,cursor=cursor)

    @app.get('/api/artists/{artist_id}')
    def page(artist_id:UUID,actor:str=Depends(identity),data=Depends(repo)):
        return data.private_page(actor,artist_id)

    @app.patch('/api/artists/{artist_id}')
    def edit(artist_id:UUID,body:dict=Body(...),actor:str=Depends(identity),data=Depends(repo)):
        return data.edit_page(actor,artist_id,body)

    @app.put('/api/artists/{artist_id}/layout')
    def layout(artist_id:UUID,body:Layout,actor:str=Depends(identity),data=Depends(repo)):
        return data.edit_layout(actor,artist_id,body)

    @app.post('/api/admin/artists/{artist_id}/owner')
    def assign(artist_id:UUID,body:AssignOwner,actor:str=Depends(identity),data=Depends(repo)):
        return data.change_owner(actor,artist_id,action='assign',target=body.account_id)

    @app.put('/api/admin/artists/{artist_id}/owner')
    def transfer(artist_id:UUID,body:TransferOwner,actor:str=Depends(identity),data=Depends(repo)):
        return data.change_owner(actor,artist_id,action='transfer',target=body.account_id,expected=body.expected_owner_account_id)

    @app.delete('/api/admin/artists/{artist_id}/owner')
    def revoke(artist_id:UUID,body:RevokeOwner,actor:str=Depends(identity),data=Depends(repo)):
        return data.change_owner(actor,artist_id,action='revoke',expected=body.expected_owner_account_id)

    @app.put('/api/artists/{artist_id}/publication')
    def publication(artist_id:UUID,body:Publication,actor:str=Depends(identity),data=Depends(repo)):
        return data.page_state(actor,artist_id,body.state)

    @app.put('/api/admin/artists/{artist_id}/suspension')
    def suspension(artist_id:UUID,body:Suspension,actor:str=Depends(identity),data=Depends(repo)):
        return data.page_state(actor,artist_id,body.state,admin_only=True)

    @app.put('/api/admin/artists/{artist_id}/verification')
    def verification(artist_id:UUID,body:Verification,actor:str=Depends(identity),data=Depends(repo)):
        return data.page_state(actor,artist_id,None,admin_only=True,verification=body.verified)

    @app.get('/api/admin/artists/{artist_id}/ownership-audit')
    def audit(artist_id:UUID,actor:str=Depends(identity),data=Depends(repo),limit:int=Query(20,ge=1,le=100),cursor:UUID|None=None):
        return data.audit(actor,artist_id,limit,cursor)

    @app.get('/api/admin/users/{user_id}/profile')
    def user_read(user_id:UUID,actor:str=Depends(identity),data=Depends(repo)):
        return data.public_user_profile(actor,user_id)

    @app.patch('/api/admin/users/{user_id}/profile')
    def user_edit(user_id:UUID,body:dict=Body(...),actor:str=Depends(identity),data=Depends(repo)):
        return data.public_user_profile(actor,user_id,body)

    def create_resource(resource):
        def endpoint(artist_id:UUID,body:dict=Body(...),actor:str=Depends(identity),data=Depends(repo)):
            return data.content_write(actor,artist_id,resource,body)
        return endpoint
    def list_resource(resource):
        def endpoint(artist_id:UUID,actor:str=Depends(identity),data=Depends(repo),limit:int=Query(20,ge=1,le=100),cursor:UUID|None=None):
            return data.content_read(actor,artist_id,resource,limit=limit,cursor=cursor)
        return endpoint
    def read_resource(resource):
        def endpoint(artist_id:UUID,content_id:UUID,actor:str=Depends(identity),data=Depends(repo)):
            return data.content_read(actor,artist_id,resource,content_id)
        return endpoint
    def edit_resource(resource):
        def endpoint(artist_id:UUID,content_id:UUID,body:dict=Body(...),actor:str=Depends(identity),data=Depends(repo)):
            return data.content_write(actor,artist_id,resource,body,content_id)
        return endpoint
    def delete_resource(resource):
        def endpoint(artist_id:UUID,content_id:UUID,actor:str=Depends(identity),data=Depends(repo)):
            return data.content_delete(actor,artist_id,resource,content_id)
        return endpoint
    def publish_resource(resource):
        def endpoint(artist_id:UUID,content_id:UUID,body:Publication,actor:str=Depends(identity),data=Depends(repo)):
            return data.content_publication(actor,artist_id,resource,content_id,body.state)
        return endpoint

    for resource in RESOURCE_MODELS:
        path='/api/artists/{artist_id}/'+resource
        if resource!='tracks':
            app.add_api_route(path,create_resource(resource),methods=['POST'],status_code=201,name=resource+'_create')
            app.add_api_route(path,list_resource(resource),methods=['GET'],name=resource+'_list')
        path+='/{content_id}'
        for method,factory in (('GET',read_resource),('PATCH',edit_resource),('DELETE',delete_resource)):
            app.add_api_route(path,factory(resource),methods=[method],name=resource+'_'+method.lower())
        if resource!='events':
            app.add_api_route(path+'/publication',publish_resource(resource),methods=['PUT'],name=resource+'_publication')
