"""Final admin_nomination/v1 wire and immutable evidence; no contact secrets/keys."""
import hashlib
import json
import re
import time
from typing import Literal, Annotated, Union
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from .auth import Identity, require_fresh_actor, unique_pairs
from .media import MediaError

Revision=Annotated[int,Field(strict=True,ge=0,le=2147483647)]
Epoch=Annotated[int,Field(strict=True,gt=0,le=9223372036854775807)]
TokenId=Annotated[str,Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')]
MAX_LEASE=86400


def canonical_uuid(value):
    if type(value) is not str or str(UUID(value))!=value: raise ValueError('Canonical UUID required')
    return value


class FrozenModel(BaseModel):
    model_config=ConfigDict(strict=True,extra='forbid',frozen=True,hide_input_in_errors=True)


class Scope(FrozenModel):
    app_id: Literal['shadify_api']
    app_instance_id: str
    domain_app: Literal['shadify']
    deployment_id: TokenId
    _uuid=field_validator('app_instance_id')(canonical_uuid)


class Selector(FrozenModel):
    email: str|None=Field(default=None,min_length=1,max_length=320)
    phone: str|None=Field(default=None,min_length=1,max_length=64)
    @model_validator(mode='after')
    def supplied(self):
        if self.email is None and self.phone is None: raise ValueError('Selector required')
        return self


class BaseOperation(FrozenModel):
    operation_id: str
    nomination_id: str
    scope: Scope
    expected_nomination_revision: Revision
    _ids=field_validator('operation_id','nomination_id')(canonical_uuid)


class SelectionOperation(BaseOperation):
    intent: Literal['initialize','recover']
    expected_admin_revision: Revision
    expected_primary_account_id: str|None
    delegate_policy: Literal['retain','revoke']|None
    expires_at: Epoch
    selector: Selector
    @model_validator(mode='after')
    def intent_shape(self):
        if self.intent=='initialize':
            if self.expected_admin_revision!=0 or self.expected_primary_account_id is not None or self.delegate_policy is not None:
                raise ValueError('Initialize expects unconfigured zero revision')
        elif (self.expected_admin_revision<=0 or not valid_identity_id(self.expected_primary_account_id)
              or self.delegate_policy is None): raise ValueError('Explicit recovery prestate required')
        return self


class Register(SelectionOperation):
    operation: Literal['register']
    @model_validator(mode='after')
    def revision(self):
        if self.expected_nomination_revision!=0: raise ValueError('Fresh registration required')
        return self


class Renew(BaseOperation):
    operation: Literal['renew']
    expires_at: Epoch
    @model_validator(mode='after')
    def revision(self):
        if self.expected_nomination_revision<=0: raise ValueError('Existing revision required')
        return self


class Revoke(BaseOperation):
    operation: Literal['revoke']
    @model_validator(mode='after')
    def revision(self):
        if self.expected_nomination_revision<=0: raise ValueError('Existing revision required')
        return self


class Replace(SelectionOperation):
    operation: Literal['replace']
    replacement_nomination_id: str
    _replacement=field_validator('replacement_nomination_id')(canonical_uuid)
    @model_validator(mode='after')
    def revision(self):
        if self.expected_nomination_revision<=0 or self.replacement_nomination_id==self.nomination_id:
            raise ValueError('Distinct fresh replacement and revision required')
        return self


Operation=Annotated[Union[Register,Renew,Revoke,Replace],Field(discriminator='operation')]
OPERATION=TypeAdapter(Operation)


def valid_identity_id(value):
    return type(value) is str and 1<=len(value)<=36 and all(33<=ord(c)<=126 for c in value)


class Tags(FrozenModel):
    email: tuple[str,...]=()
    phone: tuple[str,...]=()
    @field_validator('email','phone',mode='before')
    @classmethod
    def arrays(cls,value):
        if type(value) is not list or len(value)>64: raise ValueError('Bounded tag array required')
        versions=[]
        for tag in value:
            if type(tag) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}:[a-f0-9]{64}',tag):
                raise ValueError('Versioned tag required')
            versions.append(tag.split(':',1)[0])
        if versions!=sorted(versions) or len(set(versions))!=len(versions):
            raise ValueError('Unique ordered versions required')
        return tuple(value)


class ProjectionBase(FrozenModel):
    scope: Scope
    operation: Literal['register','renew','revoke','replace']
    operation_id: str
    nomination_id: str
    body_sha256: str=Field(pattern=r'^[a-f0-9]{64}$')
    _ids=field_validator('operation_id','nomination_id')(canonical_uuid)


class SelectionProjection(ProjectionBase):
    selector_tags: Tags
    existing_account_id: str|None
    @field_validator('existing_account_id')
    @classmethod
    def account(cls,value):
        if value is not None and not valid_identity_id(value): raise ValueError('Exact Auth account required')
        return value


class Contacts(FrozenModel):
    scope: Scope
    account_id: str
    tags: Tags
    @field_validator('account_id')
    @classmethod
    def account(cls,value):
        if not valid_identity_id(value): raise ValueError('Exact Auth account required')
        return value


class Witness(FrozenModel):
    schema_version: Literal[1]
    scope: Scope
    operation: Literal['register','renew','revoke','replace']
    operation_id: str
    nomination_id: str
    actor_account_id: str
    actor_session_id: str
    body_sha256: str=Field(pattern=r'^[a-f0-9]{64}$')
    expires_at: Epoch
    _ids=field_validator('operation_id','nomination_id')(canonical_uuid)
    @field_validator('actor_account_id','actor_session_id')
    @classmethod
    def identity(cls,value):
        if not valid_identity_id(value): raise ValueError('Exact Auth identity required')
        return value


def exact_json(raw):
    return json.loads(raw.decode('utf-8'),object_pairs_hook=unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def parse_operation(raw):
    try: return OPERATION.validate_python(exact_json(raw))
    except Exception: raise MediaError(422,'Invalid nomination operation') from None


def claims(request,actor):
    from .auth import AdmittedIdentity
    context=getattr(request.state,'admitted_identity',None)
    if type(context) is not AdmittedIdentity or context.identity!=actor:
        raise MediaError(401,'Canonical admitted assertion required')
    require_fresh_actor(actor)
    return exact_json(context.payload)


class Approval(FrozenModel):
    actor: object
    scope: Scope
    operation_id: str
    approval_fingerprint: str
    expires_at: Epoch
    witness: Witness


def registration_evidence(request,actor,raw,operation,scope,witness):
    try:
        value=claims(request,actor)
        if request.method!='POST' or request.url.path!='/api/admin/nominations' or request.url.query:
            raise ValueError()
        if actor.expires_at-actor.issued_at>60: raise ValueError()
        model=SelectionProjection if operation.operation in ('register','replace') else ProjectionBase
        projection=model.model_validate(value['admin_nomination_v1'])
        digest=hashlib.sha256(raw).hexdigest()
        if (projection.scope!=scope or operation.scope!=scope or witness.scope!=scope
                or projection.operation!=operation.operation or projection.operation_id!=operation.operation_id
                or projection.nomination_id!=operation.nomination_id or projection.body_sha256!=digest
                or witness.operation!=operation.operation or witness.operation_id!=operation.operation_id
                or witness.nomination_id!=operation.nomination_id or witness.body_sha256!=digest
                or witness.actor_account_id!=actor.account_id or witness.actor_session_id!=actor.session_id): raise ValueError()
        if operation.operation in ('register','replace'):
            for kind in ('email','phone'):
                if (getattr(operation.selector,kind) is not None)!=bool(getattr(projection.selector_tags,kind)): raise ValueError()
        approval=value['proxy_approval']
        checks={'approval_kind':'proxy','mode':'execute','account_id':actor.account_id,'session_id':actor.session_id,
                'domain_app':actor.domain_app,'scope':'admin_nomination/v1','single_use':True,'http_method':'POST',
                'proxy_path':'/admin/nominations','proxy_query':'','body_sha256':digest}
        if any(type(approval.get(k)) is not type(v) or approval[k]!=v for k,v in checks.items()): raise ValueError()
        jti=approval.get('jti');exp=approval.get('exp');iat=approval.get('iat')
        now=int(time.time())
        if (type(jti) is not str or not 1<=len(jti)<=128 or type(exp) is not int or type(iat) is not int
                or iat>now+60 or exp<=now or exp<=iat or actor.expires_at>exp or witness.expires_at<=now): raise ValueError()
        tokens=request.headers.getlist('x-proxy-approval')
        if len(tokens)!=1 or hashlib.sha256(tokens[0].encode('ascii')).hexdigest()!=value['proxy_approval_token_sha256']:
            raise ValueError()
        fingerprint=hashlib.sha256((json.dumps(scope.model_dump(),sort_keys=True,separators=(',',':'),ensure_ascii=True)+':'+jti).encode()).hexdigest()
        evidence=Approval(actor=actor,scope=scope,operation_id=operation.operation_id,approval_fingerprint=fingerprint,
                          expires_at=min(exp,actor.expires_at,witness.expires_at),witness=witness)
        return projection,evidence
    except Exception: raise MediaError(401,'Nomination confirmation binding unavailable') from None


def optional_contacts(request,actor,scope):
    try:
        value=claims(request,actor)
        if request.method!='GET' or request.url.path!='/api/me' or request.url.query or actor.expires_at-actor.issued_at>60:
            return None
        proof=Contacts.model_validate(value['verified_contacts_v1'])
        if proof.scope!=scope or proof.account_id!=actor.account_id:return None
        return proof
    except Exception:return None
