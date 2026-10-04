"""Finite Auth-admitted nomination controls and own-identity context metadata."""
import hashlib
from fastapi import Depends,Request
from .auth import require_fresh_actor
from .media import MediaError
from .nomination_contract import parse_operation,registration_evidence,optional_contacts


def install_nomination_routes(app,identity,control):
    def configured():
        if control is None:raise MediaError(503,'Nomination integration pending')
        return control

    @app.get('/api/admin/nominations/context')
    def context(request:Request,actor=Depends(identity)):
        if request.url.query:raise MediaError(422,'Context query must be empty')
        require_fresh_actor(actor)
        scope=configured().scope()
        require_fresh_actor(actor)
        return {'account_id':actor.account_id,'session_id':actor.session_id,'scope':scope.model_dump()}

    @app.post('/api/admin/nominations')
    async def operation(request:Request,actor=Depends(identity)):
        if request.url.query:raise MediaError(422,'Nomination query must be empty')
        raw=await request.body()
        require_fresh_actor(actor)
        command=parse_operation(raw)
        owner=configured();scope=owner.scope();witness=owner.witness_reader.read()
        projection,evidence=registration_evidence(request,actor,raw,command,scope,witness)
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(owner.apply,command,projection,evidence,hashlib.sha256(raw).hexdigest())


def finalize_me(control,request,actor):
    require_fresh_actor(actor)
    if control is None:return
    try:
        scope=control.scope()
        contacts=optional_contacts(request,actor,scope)
        if contacts is not None:control.finalize(actor,contacts)
    except Exception:
        # finalize owns its transaction: it has rolled back before returning
        # an error here. Optional nomination work cannot gate ordinary /me.
        require_fresh_actor(actor)
        return
    require_fresh_actor(actor)
