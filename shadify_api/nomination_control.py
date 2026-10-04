"""Final app-owned atomic nomination controls; Node/Auth evidence is paired first."""
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime,timezone
from uuid import UUID,uuid4
from typing import Protocol
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg.errors import UniqueViolation
from .admin import AppAdminRepository,active_identity
from .auth import require_fresh_actor
from .media import MediaError
from .nomination_contract import Scope,Witness,Approval,MAX_LEASE,ControlReceipt,OperationResultV1,valid_identity_id


class NominationProvider(Protocol):
    """Installed Node verifier supplies evidence; API supplies SQL status reads."""
    def read_verified_witness(self,installed_scope:Scope,operation_id:str)->Witness: ...
    def require_current(self,installed_selection:Scope,witness:Witness|None=None)->None: ...


class UnavailableNominationProvider:
    """No protected Node transport is implicitly authenticated by a file."""
    def read_verified_witness(self,installed_scope,operation_id):
        raise MediaError(503,'Protected nomination provider unavailable')
    def require_current(self,installed_selection,witness=None):
        raise MediaError(503,'Protected nomination provider unavailable')


@dataclass(frozen=True)
class AuditActor:
    account_id:str
    user_id:UUID


class NominationControl(AppAdminRepository):
    def __init__(self,connect,scope_provider,provider:NominationProvider|None=None):
        self.connect,self.scope_provider=connect,scope_provider
        self.provider=provider if provider is not None else UnavailableNominationProvider()
    def scope(self):
        scope=self.scope_provider()
        if type(scope) is not Scope:raise MediaError(503,'Installed nomination scope unavailable')
        return scope
    def require_current(self,installed_selection,witness=None):
        if type(installed_selection) is not Scope or installed_selection!=self.scope():
            raise MediaError(403,'Nomination scope mismatch')
        if witness is not None and (type(witness) is not Witness or witness.scope!=installed_selection):
            raise MediaError(401,'Verified nomination witness required')
        # Provider owns protected producer verification, not browser callbacks.
        if self.provider.require_current(installed_selection,witness) is not None:
            raise MediaError(503,'Protected nomination provider contract invalid')
        if installed_selection!=self.scope():
            raise MediaError(409,'Installed nomination selection changed')

    def read_verified_witness(self,installed_scope,operation_id):
        from .nomination_contract import canonical_uuid
        try:canonical_uuid(operation_id)
        except (ValueError,TypeError,AttributeError):raise MediaError(422,'Canonical operation UUID required') from None
        self.require_current(installed_scope)
        witness=self.provider.read_verified_witness(installed_scope,operation_id)
        if (type(witness) is not Witness or witness.scope!=installed_scope
                or witness.operation_id!=operation_id or witness.expires_at<=int(time.time())):
            raise MediaError(401,'Verified nomination witness unavailable')
        self.require_current(installed_scope,witness)
        if witness.expires_at<=int(time.time()):raise MediaError(401,'Verified nomination witness expired')
        return witness

    def ready(self):
        self.require_current(self.scope())
        with self.connect() as c:
            if c.execute('SELECT nomination_id FROM shadify_nomination_control WHERE singleton').fetchone() is None:
                raise MediaError(503,'Nomination schema unavailable')
            c.execute('SELECT scope,email_tags,phone_tags,existing_account_id FROM shadify_admin_nomination_v1 LIMIT 0')
            c.execute('SELECT operation_id,approval_fingerprint FROM shadify_nomination_operations LIMIT 0')
            c.execute('SELECT operation_id,scope,result,revision,state,expires_at FROM shadify_nomination_operation_status LIMIT 0')
    def _fresh(self,evidence):
        require_fresh_actor(evidence.actor)
        self.require_current(evidence.scope,evidence.witness)
        if self.read_verified_witness(evidence.scope,evidence.operation_id)!=evidence.witness:
            raise MediaError(409,'Protected nomination witness changed')
        require_fresh_actor(evidence.actor)
        if evidence.expires_at<=int(time.time()):
            raise MediaError(401,'Nomination confirmation expired')
    def _lease(self,expiry):
        now=int(time.time())
        if not now<expiry<=now+MAX_LEASE:raise MediaError(422,'Nomination expiry outside24hour bound')
    def _primary(self,c,state):
        if state['primary_user_id'] is None:return None
        row=c.execute('SELECT auth_account_id FROM shadify_users WHERE id=%s',(state['primary_user_id'],)).fetchone()
        if row is None:raise MediaError(503,'Primary identity unavailable')
        return row['auth_account_id']
    def _prestate(self,c,state,revision,primary):
        if state['revision']!=revision or self._primary(c,state)!=primary:
            raise MediaError(409,'Admin prestate changed')
    def _state(self,row):
        if row['state']=='pending' and row['expires_at']<=datetime.now(timezone.utc):return 'expired'
        return row['state']
    def _dto(self,row):
        return {'nomination_id':str(row['id']),'revision':row['revision'],'state':self._state(row),
                'intent':row['intent'],'expires_at':row['expires_at'].isoformat()}
    def _row(self,c,nomination_id,scope):
        row=c.execute('SELECT * FROM shadify_admin_nomination_v1 WHERE id=%s FOR UPDATE',(UUID(nomination_id),)).fetchone()
        if row is None:raise MediaError(404,'Nomination not found')
        if row['scope']!=scope.model_dump():raise MediaError(403,'Nomination scope mismatch')
        return row
    def _insert(self,c,operation,projection,nomination_id,predecessor=None):
        return c.execute('INSERT INTO shadify_admin_nomination_v1 '
              '(id,scope,state,intent,expected_admin_revision,expected_primary_account_id,delegate_policy,email_tags,phone_tags,existing_account_id,expires_at,predecessor_id) '
              "VALUES(%s,%s,'pending',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
              (UUID(nomination_id),Jsonb(operation.scope.model_dump()),operation.intent,operation.expected_admin_revision,
               operation.expected_primary_account_id,operation.delegate_policy,list(projection.selector_tags.email),
               list(projection.selector_tags.phone),projection.existing_account_id,
               datetime.fromtimestamp(operation.expires_at,timezone.utc),predecessor)).fetchone()
    def apply(self,operation,projection,evidence,body_sha256):
        if type(evidence) is not Approval or evidence.operation_id!=operation.operation_id:
            raise MediaError(401,'Paired nomination evidence required')
        self._fresh(evidence)
        try:
            with self.connect() as connection:
                with connection.cursor(row_factory=dict_row) as c:
                    state=self._admin_state(c,exclusive=True)
                    slot=c.execute('SELECT * FROM shadify_nomination_control WHERE singleton FOR UPDATE').fetchone()
                    if slot is None:raise MediaError(503,'Nomination schema unavailable')
                    actor=c.execute('SELECT * FROM shadify_users WHERE auth_account_id=%s FOR UPDATE',(evidence.actor.account_id,)).fetchone()
                    if not active_identity(actor):raise MediaError(403,'Active admitted human required')
                    audit_actor=AuditActor(evidence.actor.account_id,actor['id'])
                    self._fresh(evidence)
                    before=None
                    if operation.operation=='register':
                        if slot['nomination_id'] is not None:
                            history=self._row(c,str(slot['nomination_id']),evidence.scope)
                            if self._state(history) not in ('claimed','revoked'):
                                raise MediaError(409,'Use explicit replace for outstanding nomination')
                        self._lease(operation.expires_at)
                        self._prestate(c,state,operation.expected_admin_revision,operation.expected_primary_account_id)
                        row=self._insert(c,operation,projection,operation.nomination_id)
                        c.execute('UPDATE shadify_nomination_control SET nomination_id=%s WHERE singleton',(row['id'],))
                    else:
                        before=self._row(c,operation.nomination_id,evidence.scope)
                        if slot['nomination_id']!=before['id'] or before['revision']!=operation.expected_nomination_revision:
                            raise MediaError(409,'Nomination selection or revision changed')
                        status=self._state(before)
                        if operation.operation=='renew':
                            if status!='pending':raise MediaError(409,'Only unexpired pending nomination can renew')
                            self._prestate(c,state,before['expected_admin_revision'],before['expected_primary_account_id'])
                            self._lease(operation.expires_at)
                            if operation.expires_at<=int(before['expires_at'].timestamp()):raise MediaError(422,'Renew must extend expiry')
                            row=c.execute('UPDATE shadify_admin_nomination_v1 SET expires_at=%s,revision=revision+1 WHERE id=%s RETURNING *',
                                          (datetime.fromtimestamp(operation.expires_at,timezone.utc),before['id'])).fetchone()
                        elif operation.operation=='revoke':
                            # Neither contact lookup nor current primary prestate is needed.
                            if status not in ('pending','expired'):raise MediaError(409,'Nomination already terminal')
                            row=c.execute("UPDATE shadify_admin_nomination_v1 SET state='revoked',revision=revision+1 WHERE id=%s RETURNING *",(before['id'],)).fetchone()
                        else:
                            if status not in ('pending','expired','blocked'):raise MediaError(409,'Nomination cannot be replaced')
                            self._lease(operation.expires_at)
                            self._prestate(c,state,operation.expected_admin_revision,operation.expected_primary_account_id)
                            # Fresh review binds the replacement's current admin prestate;
                            # predecessor revision remains exact even when blocked by drift.
                            if status=='pending':
                                c.execute("UPDATE shadify_admin_nomination_v1 SET state='replaced',revision=revision+1 WHERE id=%s AND expires_at>clock_timestamp()",(before['id'],))
                            row=self._insert(c,operation,projection,operation.replacement_nomination_id,before['id'])
                            c.execute('UPDATE shadify_nomination_control SET nomination_id=%s WHERE singleton',(row['id'],))
                    result={'operation_id':operation.operation_id,'operation':operation.operation,**self._dto(row)}
                    c.execute('INSERT INTO shadify_nomination_operations '
                              '(operation_id,nomination_id,scope,operation,actor_account_id,body_sha256,approval_fingerprint,result) '
                              'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                              (UUID(operation.operation_id),row['id'],Jsonb(evidence.scope.model_dump()),operation.operation,
                               evidence.actor.account_id,body_sha256,evidence.approval_fingerprint,Jsonb(result)))
                    self._admin_audit(c,audit_actor,'nomination_v1_'+operation.operation,
                                      details={'operation_id':operation.operation_id,'nomination_id':str(row['id']),
                                               'predecessor_id':str(before['id']) if before else None,
                                               'before_revision':before['revision'] if before else 0,'after_revision':row['revision'],
                                               'intent':row['intent'],'state':self._state(row)})
                    self._fresh(evidence)
                    if operation.operation!='revoke' and row['expires_at']<=datetime.now(timezone.utc):
                        raise MediaError(409,'Nomination expired before commit')
                    return result
        except UniqueViolation:raise MediaError(409,'Nomination ID, operation or approval already used') from None
    def finalize(self,actor,contacts):
        if contacts is None:return False
        require_fresh_actor(actor)
        scope=self.scope()
        self.require_current(scope)
        require_fresh_actor(actor)
        if contacts.scope!=scope or contacts.account_id!=actor.account_id:return False
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                state=self._admin_state(c,exclusive=True)
                slot=c.execute('SELECT nomination_id FROM shadify_nomination_control WHERE singleton FOR UPDATE').fetchone()
                if slot is None:raise MediaError(503,'Nomination schema unavailable')
                if slot['nomination_id'] is None:return False
                row=self._row(c,str(slot['nomination_id']),scope)
                if self._state(row)!='pending':return False
                if row['existing_account_id'] is not None and row['existing_account_id']!=actor.account_id:return False
                for kind in ('email','phone'):
                    selected=row[kind+'_tags']
                    if selected and not set(selected).intersection(getattr(contacts.tags,kind)):return False
                user=c.execute('SELECT * FROM shadify_users WHERE auth_account_id=%s FOR UPDATE',(actor.account_id,)).fetchone()
                if not active_identity(user):return False
                require_fresh_actor(actor)
                self.require_current(scope)
                require_fresh_actor(actor)
                audit_actor=AuditActor(actor.account_id,user['id'])
                try:self._prestate(c,state,row['expected_admin_revision'],row['expected_primary_account_id'])
                except MediaError:
                    c.execute("UPDATE shadify_admin_nomination_v1 SET state='blocked',revision=revision+1 WHERE id=%s",(row['id'],))
                    self._admin_audit(c,audit_actor,'nomination_v1_blocked',details={'nomination_id':str(row['id'])})
                    require_fresh_actor(actor)
                    self.require_current(scope)
                    require_fresh_actor(actor)
                    return False
                previous=c.execute('SELECT u.auth_account_id,d.permissions FROM shadify_app_admin_delegates d '
                                   'JOIN shadify_users u ON u.id=d.user_id ORDER BY u.auth_account_id').fetchall()
                if row['intent']=='initialize':
                    if previous:raise MediaError(409,'Unexpected unconfigured delegate state')
                else:
                    if row['expected_primary_account_id']==actor.account_id:return False
                    if row['delegate_policy']=='revoke':c.execute('DELETE FROM shadify_app_admin_delegates')
                    else:c.execute('DELETE FROM shadify_app_admin_delegates WHERE user_id=%s',(user['id'],))
                c.execute('UPDATE shadify_app_admin SET primary_user_id=%s,revision=revision+1 WHERE singleton',(user['id'],))
                c.execute("UPDATE shadify_admin_nomination_v1 SET state='claimed',claimed_user_id=%s,revision=revision+1 WHERE id=%s",(user['id'],row['id']))
                self._admin_audit(c,audit_actor,'nomination_v1_claim',target=actor.account_id,
                                  details={'nomination_id':str(row['id']),'intent':row['intent'],
                                           'old_primary':row['expected_primary_account_id'],'delegate_policy':row['delegate_policy'],
                                           'previous_delegates':previous})
                require_fresh_actor(actor)
                self.require_current(scope)
                require_fresh_actor(actor)
                if row['expires_at']<=datetime.now(timezone.utc):
                    raise MediaError(401,'Nomination evidence expired or scope changed before commit')
                return True
    def read_committed_operation(self,installed_scope,operation_id):
        """Immutable original control receipt, never current rights/state."""
        import re
        from .nomination_contract import canonical_uuid
        try:canonical_uuid(operation_id)
        except (ValueError,TypeError,AttributeError):raise MediaError(422,'Canonical operation UUID required') from None
        self.require_current(installed_scope)
        result=OperationResultV1(scope=installed_scope,operation_id=operation_id,
                                 outcome='unknown',operation=None,receipt=None)
        try:
            with self.connect() as connection:
                connection.execute('SET TRANSACTION READ ONLY')
                with connection.cursor(row_factory=dict_row) as c:
                    row=c.execute(
                        "SELECT o.*, (SELECT count(*) FROM shadify_app_admin_audit a "
                        "WHERE a.action='nomination_v1_'||o.operation "
                        "AND a.actor_account_id=o.actor_account_id "
                        "AND a.details->>'operation_id'=o.operation_id::text "
                        "AND a.details->>'nomination_id'=o.nomination_id::text) AS audit_count "
                        "FROM shadify_nomination_operations o WHERE o.operation_id=%s AND o.scope=%s",
                        (UUID(operation_id),Jsonb(installed_scope.model_dump()))).fetchone()
                    if row is None:
                        result=OperationResultV1(scope=installed_scope,operation_id=operation_id,
                                                 outcome='not_found',operation=None,receipt=None)
                    else:
                        stored=row['result']
                        if (row['audit_count']!=1 or not valid_identity_id(row['actor_account_id'])
                                or not re.fullmatch(r'[a-f0-9]{64}',row['approval_fingerprint'])
                                or not re.fullmatch(r'[a-f0-9]{64}',row['body_sha256'])
                                or type(stored) is not dict
                                or set(stored)!={'operation_id','operation','nomination_id','revision','state','intent','expires_at'}
                                or stored['operation_id']!=operation_id or str(row['operation_id'])!=operation_id
                                or stored['nomination_id']!=str(row['nomination_id'])
                                or stored['operation']!=row['operation']
                                or stored['intent'] not in ('initialize','recover')):
                            raise ValueError('Unproven committed operation')
                        receipt=ControlReceipt(operation_id=operation_id,nomination_id=stored['nomination_id'],
                                               status=stored['state'],revision=stored['revision'])
                        result=OperationResultV1(scope=installed_scope,operation_id=operation_id,
                                                 outcome='committed',operation=row['operation'],receipt=receipt)
        except Exception:
            # Read/format/cleanup failure cannot manufacture committed success.
            result=OperationResultV1(scope=installed_scope,operation_id=operation_id,
                                     outcome='unknown',operation=None,receipt=None)
        self.require_current(installed_scope)
        return result

    def read_operation_result(self,installed_scope,operation_id):
        """Current nomination metadata only; NOT the immutable Node receipt."""
        from .nomination_contract import canonical_uuid
        try:canonical_uuid(operation_id)
        except (ValueError,TypeError,AttributeError):raise MediaError(422,'Canonical operation UUID required') from None
        self.require_current(installed_scope)
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                row=c.execute('SELECT * FROM shadify_nomination_operation_status WHERE operation_id=%s AND scope=%s',
                              (UUID(operation_id),Jsonb(installed_scope.model_dump()))).fetchone()
        self.require_current(installed_scope)
        if row is None:return None
        return {**row['result'],'revision':row['revision'],'state':row['state'],'expires_at':row['expires_at'].isoformat()}

    def operation_result(self,operation_id):
        # Existing app-owned internal call, still no public HTTP status route.
        return self.read_operation_result(self.scope(),operation_id)
