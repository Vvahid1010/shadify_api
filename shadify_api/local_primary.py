"""Finite unwired WSL primary seam. Operational authority belongs to local-control."""
import hashlib
import json
import time
from datetime import datetime
from typing import Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from psycopg import sql
from psycopg.rows import dict_row
from .admin import active_identity
from .media import MediaError
from .nomination_contract import canonical_uuid, valid_identity_id


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True, hide_input_in_errors=True)


class ExistingMapping(Closed):
    app_id: Literal['shadify_api']
    domain_app: Literal['shadify']
    account_id: str
    local_user_id: str

    @field_validator('account_id')
    @classmethod
    def account(cls, value):
        if not valid_identity_id(value):raise ValueError('Exact existing account reference required')
        return value

    @field_validator('local_user_id')
    @classmethod
    def local_id(cls, value):
        canonical_uuid(value)
        return value


class LocalPrimaryCommand(Closed):
    operation: Literal['initialize-primary','recover-primary']
    operation_id: str
    mode: Literal['dry-run','apply'] = 'dry-run'
    authority_reference: str = Field(min_length=1,max_length=200)
    binding_reference: str = Field(min_length=1,max_length=200)
    mapping: ExistingMapping
    expected_admin_revision: int = Field(ge=0,le=9223372036854775807)
    expected_primary_account_id: str | None = None
    delegates: Literal['retain','revoke'] | None = None

    @field_validator('operation_id')
    @classmethod
    def operation_uuid(cls,value):
        canonical_uuid(value)
        return value

    @model_validator(mode='after')
    def explicit_recovery(self):
        if self.operation=='initialize-primary':
            if self.expected_primary_account_id is not None or self.delegates is not None:
                raise ValueError('Initialization has no prior primary or delegate policy')
        elif not valid_identity_id(self.expected_primary_account_id) or self.delegates is None:
            raise ValueError('Recovery requires exact prior primary and explicit delegate policy')
        return self


class WSLBinding(Closed):
    environment: Literal['wsl']
    app_id: Literal['shadify_api']
    domain_app: Literal['shadify']
    node_id: str
    app_instance_id: str
    reference: str = Field(min_length=1,max_length=200)
    database_name: str = Field(min_length=1,max_length=63)
    database_oid: int = Field(ge=1,le=4294967295)
    schema_name: str = Field(pattern=r'^[a-z_][a-z0-9_]{0,62}$')
    postmaster_started_at: datetime
    server_address: str | None
    server_port: int | None = Field(ge=1,le=65535)

    @field_validator('node_id','app_instance_id')
    @classmethod
    def instance_uuid(cls,value):
        canonical_uuid(value)
        return value


class VerifiedLocalContext(Closed):
    binding: WSLBinding
    request_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    authority_audit_reference: str = Field(min_length=1,max_length=200)
    expires_at: int = Field(ge=1)
    immutable_executable: bool


class LocalPrimaryContextProvider(Protocol):
    """Pure protected verification, supplied by local-control; never CLI choices.

    admit independently authenticates authority/reference, WSL binding and
    approved immutable executable for the entire command/mode. Neither callback
    may write even during dry-run. require_current rereads actual authority and
    installed binding/source; returning a declaration or True is insufficient.
    No common operational policy or reference path is selected by this app.
    """
    def admit(self,command:LocalPrimaryCommand)->VerifiedLocalContext: ...
    def require_current(self,command:LocalPrimaryCommand,context:VerifiedLocalContext)->None: ...


def command_digest(command):
    return hashlib.sha256(json.dumps(command.model_dump(mode='json'),sort_keys=True,
        separators=(',',':'),ensure_ascii=True).encode()).hexdigest()


DATABASE_FACTS = ("SELECT current_database() AS database_name, "
    "(SELECT oid FROM pg_database WHERE datname=current_database()) AS database_oid, "
    "current_schema() AS schema_name, pg_postmaster_start_time() AS postmaster_started_at, "
    "inet_server_addr()::text AS server_address, inet_server_port() AS server_port")


class LocalPrimaryAdapter:
    def __init__(self,repository,*,context_provider=None,native_identity_provider=None):
        self.repository=repository
        self.context_provider=context_provider
        self.native_identity_provider=native_identity_provider

    def _current(self,command,context):
        if type(context) is not VerifiedLocalContext:
            raise MediaError(403,'Verified local primary context required')
        try:
            VerifiedLocalContext.model_validate(context.model_dump(mode='python'))
        except ValueError:
            raise MediaError(403,'Verified local primary context required') from None
        if (type(context) is not VerifiedLocalContext or type(context.binding) is not WSLBinding
                or context.request_sha256!=command_digest(command)
                or context.binding.reference!=command.binding_reference
                or context.immutable_executable is not True or int(time.time())>=context.expires_at):
            raise MediaError(403,'Verified local primary context required')
        binding=context.binding
        actual=self.native_identity_provider()
        if type(actual) is not tuple or actual!=(binding.node_id,binding.app_id,binding.app_instance_id):
            raise MediaError(403,'Protected WSL instance mismatch')
        if self.context_provider.require_current(command,context) is not None:
            raise MediaError(403,'Verified local primary context required')
        if (self.native_identity_provider()!=actual or int(time.time())>=context.expires_at):
            raise MediaError(403,'Protected local context changed or expired')

    def _database(self,c,binding):
        facts=c.execute(DATABASE_FACTS).fetchone()
        if any(facts[key]!=getattr(binding,key) for key in facts):
            raise MediaError(403,'Protected WSL database mismatch')
        # Existing protected connection only; never caller DSN/env/retargeting.
        c.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(binding.schema_name)))

    def _mapping(self,c,command,*,locked):
        user=c.execute('SELECT * FROM shadify_users WHERE auth_account_id=%s'+
            (' FOR UPDATE' if locked else ''),(command.mapping.account_id,)).fetchone()
        if (not active_identity(user) or user['last_admitted_at'] is None
                or str(user['id'])!=command.mapping.local_user_id):
            raise MediaError(422,'Existing active canonical Shadify mapping required')
        return user

    def execute(self,command):
        if type(command) is not LocalPrimaryCommand:
            raise MediaError(422,'Closed local primary command required')
        try:
            LocalPrimaryCommand.model_validate(command.model_dump(mode='python'))
        except ValueError:
            raise MediaError(422,'Closed local primary command required') from None
        if self.context_provider is None or self.native_identity_provider is None:
            raise MediaError(503,'Protected local primary authority unavailable')
        # Each apply freshly admits itself; dry-run is not a reusable capability.
        context=self.context_provider.admit(command)
        self._current(command,context)
        apply=command.mode=='apply'
        with self.repository.connect() as connection:
            if not apply:connection.execute('SET TRANSACTION READ ONLY')
            with connection.cursor(row_factory=dict_row) as c:
                self._database(c,context.binding)
                state=(self.repository._admin_state(c,exclusive=True) if apply else
                    c.execute('SELECT * FROM shadify_app_admin WHERE singleton').fetchone())
                if state is None:raise MediaError(503,'Existing app admin schema required')
                user=self._mapping(c,command,locked=apply)
                primary=None
                if state['primary_user_id'] is not None:
                    prior=c.execute('SELECT auth_account_id,first_admitted_at,admission_source FROM shadify_users WHERE id=%s',
                        (state['primary_user_id'],)).fetchone()
                    if prior is None or prior['first_admitted_at'] is None or prior['admission_source']!='canonical_shell':
                        raise MediaError(503,'Existing primary mapping requires review')
                    primary=prior['auth_account_id']
                delegates=c.execute('SELECT u.auth_account_id,d.permissions FROM shadify_app_admin_delegates d '
                    'JOIN shadify_users u ON u.id=d.user_id ORDER BY u.auth_account_id').fetchall()
                eligible=(state['revision']==command.expected_admin_revision and
                    (primary is None and not delegates if command.operation=='initialize-primary' else
                     primary==command.expected_primary_account_id))
                self._current(command,context)
                self._database(c,context.binding)
                self._current(command,context)
                if not apply:
                    return {'schema_version':1,'operation_id':command.operation_id,'operation':command.operation,
                        'mode':'dry-run','outcome':'eligible' if eligible else 'denied',
                        'primary_account_id':primary,'revision':state['revision'],'audit_id':None}
                if not eligible:raise MediaError(409,'Local primary prestate changed or ineligible')
                if command.delegates=='revoke':c.execute('DELETE FROM shadify_app_admin_delegates')
                else:c.execute('DELETE FROM shadify_app_admin_delegates WHERE user_id=%s',(user['id'],))
                changed=c.execute('UPDATE shadify_app_admin SET primary_user_id=%s,revision=revision+1 '
                    'WHERE singleton AND revision=%s RETURNING revision',(user['id'],state['revision'])).fetchone()
                if changed is None:raise MediaError(409,'Local primary prestate changed')
                audit=self.repository._admin_audit(c,None,'local_'+command.operation.replace('-','_'),
                    target=command.mapping.account_id,details={'operation_id':command.operation_id,
                        'authority_reference':context.authority_audit_reference,'old_primary':primary,
                        'expected_revision':state['revision'],'revision':changed['revision'],
                        'delegates':command.delegates,'previous_delegates':delegates})
                self._mapping(c,command,locked=True)
                self._current(command,context)
                self._database(c,context.binding)
                self._current(command,context)
                return {'schema_version':1,'operation_id':command.operation_id,'operation':command.operation,
                    'mode':'apply','outcome':'committed','primary_account_id':command.mapping.account_id,
                    'revision':changed['revision'],'audit_id':str(audit)}
