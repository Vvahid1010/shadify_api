"""Private app-owned transaction seam; no HTTP/CLI or production trust issuer."""
import hashlib
import json
import re
from uuid import UUID, uuid4
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .admin import active_identity

class PrimaryChange(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    operation_id: UUID
    instance_id: UUID
    node_id: UUID
    domain_app: Literal['shadify']
    factor_scope_id: UUID
    target_user_id: UUID
    expected_primary_user_id: UUID | None
    expected_revision: StrictInt=Field(ge=0,lt=9223372036854775807)
    expires_at: StrictInt=Field(gt=0)

def commitment(change):
    return hashlib.sha256(json.dumps(change.model_dump(mode='json'),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def unavailable_admission(change,*,purpose):
    raise HTTPException(503,'primary_control_authority_unavailable')

def check(admission,change,purpose):
    kind,fingerprint=admission(change,purpose=purpose)
    if kind not in ('node','dev_operator') or type(fingerprint) is not str or not re.fullmatch('[a-f0-9]{64}',fingerprint):
        raise HTTPException(403,'primary_control_denied')
    return kind,fingerprint

def observe_result(connection,*,change,admission=unavailable_admission):
    check(admission,change,'result')
    with connection.cursor(row_factory=dict_row) as c:
        row=c.execute('SELECT * FROM shadify_primary_operations WHERE operation_id=%s',(change.operation_id,)).fetchone()
        check(admission,change,'result')
        if row is None:return {'state':'unknown'}
        if row['request_sha256']!=commitment(change):raise HTTPException(409,'primary_operation_conflict')
        return row['result']

def apply_primary(connection,*,change,admission=unavailable_admission):
    # Caller owns transaction; any exception MUST roll it back.
    kind,fingerprint=check(admission,change,'apply')
    with connection.cursor(row_factory=dict_row) as c:
        state=c.execute('SELECT * FROM shadify_app_admin WHERE singleton=true FOR UPDATE').fetchone()
        check(admission,change,'apply')
        if state is None:raise HTTPException(503,'app_admin_scope_unavailable')
        row=c.execute('SELECT * FROM shadify_primary_operations WHERE operation_id=%s',(change.operation_id,)).fetchone()
        if row:
            if row['request_sha256']!=commitment(change):raise HTTPException(409,'primary_operation_conflict')
            check(admission,change,'apply')
            return row['result']
        if state['revision']!=change.expected_revision or state['primary_user_id']!=change.expected_primary_user_id:
            raise HTTPException(409,'primary_revision_changed')
        user=c.execute('SELECT * FROM shadify_users WHERE id=%s FOR UPDATE',(change.target_user_id,)).fetchone()
        projection=c.execute('SELECT * FROM auth_identity_projections WHERE user_id=%s AND domain_app=%s AND factor_scope_id=%s FOR UPDATE',
            (change.target_user_id,change.domain_app,str(change.factor_scope_id))).fetchone()
        check(admission,change,'apply')
        if not active_identity(user) or projection is None or projection['identity_status']!='active':
            raise HTTPException(422,'primary_target_not_active_in_scope')
        grants=c.execute('SELECT user_id,permissions FROM shadify_app_admin_delegates WHERE user_id=ANY(%s) ORDER BY user_id',
            ([v for v in (state['primary_user_id'],user['id']) if v is not None],)).fetchall()
        snapshots=[dict(user_id=str(g['user_id']),permissions=g['permissions']) for g in grants]
        result=dict(state='applied',operation_id=str(change.operation_id),domain_app=change.domain_app,
            primary_user_id=str(user['id']),revision=state['revision']+1)
        c.execute('UPDATE shadify_app_admin SET primary_user_id=%s,revision=revision+1 WHERE singleton=true',(user['id'],))
        c.execute('INSERT INTO shadify_app_admin_audit(id,actor_user_id,actor_account_id,target_account_id,action,details) VALUES (%s,NULL,NULL,%s,%s,%s)',
            (uuid4(),user['auth_account_id'],'protected_primary_change',Jsonb(dict(operation_id=str(change.operation_id),authority_kind=kind,
             authority_sha256=fingerprint,previous_primary_user_id=str(state['primary_user_id']) if state['primary_user_id'] else None,
             revision=result['revision'],before_delegates=snapshots,after_delegates=snapshots))))
        c.execute('INSERT INTO shadify_primary_operations(operation_id,request_sha256,authority_sha256,authority_kind,result) VALUES (%s,%s,%s,%s,%s)',
            (change.operation_id,commitment(change),fingerprint,kind,Jsonb(result)))
        check(admission,change,'apply')
        return result
