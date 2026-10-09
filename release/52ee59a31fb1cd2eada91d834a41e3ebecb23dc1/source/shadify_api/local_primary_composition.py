"""Unwired protected composition for the frozen common selection interface.

No operational module, policy, binding, factory or account is selected here.
Only the protected internal owner may supply these dependencies.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
from psycopg import sql
from psycopg.rows import dict_row
from .admin import active_identity
from .local_primary import (LocalPrimaryAdapter, LocalPrimaryCommand, WSLBinding,
    VerifiedLocalContext, DATABASE_FACTS, command_digest)
from .media import MediaError
from .repository import PostgresMediaRepository

COMMON_COMMIT = '7701286b9e434d0f5f2b33dd14c3cfb4cc9a2925'
COMMON_SOURCE_SHA256 = 'ea20ef67109dc6c20aa23aa8e168d28aed063a74fd5ce667aeb72167c67d06fa'


@dataclass(frozen=True)
class ProtectedCompositionBinding:
    database: WSLBinding
    target: object  # Exact common BoundTargetV1 from protected domain binding.
    connection_factory: object  # Fixed owner-selected factory, never caller DSN.


class _TransactionProvider:
    def __init__(self, common, policy, binding, native):
        self.common=common;self.policy=policy;self.binding=binding;self.native=native
        self.connection=None;self.original=None;self.command=None;self.selection_request=None

    def actual_target(self):
        # Derive proof of the descriptor from this transaction's actual connection.
        database=self.binding.database;common=self.common;target=self.binding.target
        with self.connection.cursor(row_factory=dict_row) as c:
            facts=c.execute(DATABASE_FACTS).fetchone()
        if (any(facts[key]!=getattr(database,key) for key in facts)
                or self.native()!=(database.node_id,database.app_id,database.app_instance_id)
                or type(target) is not common.BoundTargetV1 or not target.valid()
                or target.app_id!=database.app_id or target.instance_ref!=database.app_instance_id):
            raise MediaError(403,'Protected WSL transaction mismatch')
        # Opaque database/DNS references remain the protected owner's selection;
        # physical DB facts and native instance were independently compared above.
        return common.BoundTargetV1(target.app_id,database.app_instance_id,
            target.database_ref,target.domain,target.environment)

    def translate(self,command):
        common=self.common
        if command.operation=='initialize-primary' and command.expected_admin_revision!=0:
            raise MediaError(422,'Initialization requires revision zero')
        payload=dict(contract=common.CONTRACT,app_id=command.mapping.app_id,
            operation=command.operation,authority_ref=command.authority_reference,
            binding_ref=command.binding_reference,mapping_ref=command.mapping.local_user_id,
            mode=command.mode)
        if command.operation=='recover-primary':
            payload.update(expected_primary=command.expected_primary_account_id,
                expected_revision=command.expected_admin_revision,delegate_policy=command.delegates)
        try:
            return common.SelectionRequestV1(common.RequestV1.parse(payload),command.mapping.domain_app,
                common.AuthLocalMappingV1(command.mapping.account_id,command.mapping.local_user_id))
        except ValueError:
            raise MediaError(422,'Unsupported common selection references') from None

    def verify(self):
        actual=self.actual_target()
        # Observe canonical mapping in this same transaction, not caller proof.
        with self.connection.cursor(row_factory=dict_row) as c:
            user=c.execute(sql.SQL('SELECT * FROM {} WHERE auth_account_id=%s').format(
                sql.Identifier(self.binding.database.schema_name,'shadify_users')),
                (self.command.mapping.account_id,)).fetchone()
        if (not active_identity(user) or user['last_admitted_at'] is None
                or str(user['id'])!=self.command.mapping.local_user_id):
            raise MediaError(422,'Existing active canonical Shadify mapping required')
        request=self.common.SelectionRequestV1(self.selection_request.request,
            self.selection_request.domain_app,
            self.common.AuthLocalMappingV1(user['auth_account_id'],str(user['id'])))
        try:
            return self.common.verify_selection_v1(request,actual,
                policy=self.policy,original=self.original)
        except self.common.SelectionDeniedV1:
            raise MediaError(403,'Protected WSL selection denied') from None

    def admit(self,command):
        self.command=command;self.selection_request=self.translate(command)
        # The original verifier object is retained exactly; no new selection/proof
        # or expiry is minted. DecisionV1 is never consumed.
        self.original=self.verify()
        selected=self.original
        return VerifiedLocalContext(binding=self.binding.database,request_sha256=command_digest(command),
            authority_audit_reference=selected.operator_audit_ref,
            audit_authority_sha256=selected.audit_authority_sha256,
            expires_at=selected.authority_expires_at,immutable_executable=True)

    def require_current(self,command,context):
        if (command!=self.command or context.request_sha256!=command_digest(self.command)
                or context.expires_at!=self.original.authority_expires_at
                or context.authority_audit_reference!=self.original.operator_audit_ref
                or context.audit_authority_sha256!=self.original.audit_authority_sha256):
            raise MediaError(403,'Protected WSL selection changed')
        self.verify()  # Fresh actual connection + common verification, same original.


class _TransactionAdapter(LocalPrimaryAdapter):
    def _admit(self,command,connection):
        self.context_provider.connection=connection
        return super()._admit(command,connection)


class LocalPrimaryComposition:
    def __init__(self,*,common=None,policy=None,binding=None,native_identity_provider=None):
        self.common=common;self.policy=policy;self.binding=binding;self.native=native_identity_provider

    def execute(self,command):
        if (self.common is None or self.policy is None or self.binding is None or self.native is None):
            raise MediaError(503,'Protected WSL composition unavailable')
        if type(command) is not LocalPrimaryCommand or type(self.binding) is not ProtectedCompositionBinding:
            raise MediaError(422,'Closed protected WSL composition required')
        try:
            # Exact frozen source; no source-tree path or import selector is shipped.
            source=Path(self.common.__file__).read_bytes()
            database=WSLBinding.model_validate(self.binding.database.model_dump(mode='python'))
            target=self.binding.target
            valid=(hashlib.sha256(source).hexdigest()==COMMON_SOURCE_SHA256
                and type(target) is self.common.BoundTargetV1 and target.valid()
                and target.app_id=='shadify_api' and target.instance_ref==database.app_instance_id
                and database.reference==command.binding_reference
                and callable(self.binding.connection_factory))
        except (AttributeError,ValueError,OSError):valid=False
        if not valid:raise MediaError(403,'Protected WSL binding unavailable')
        provider=_TransactionProvider(self.common,self.policy,self.binding,self.native)
        # Reject unsupported grammar/prestate before opening the fixed factory.
        provider.translate(command)
        repository=PostgresMediaRepository(self.binding.connection_factory)
        adapter=_TransactionAdapter(repository,context_provider=provider,native_identity_provider=self.native)
        return adapter.execute(command)
