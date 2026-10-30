"""Private contact-v2 domain transaction; production Node/DEV authority stays closed."""
import hashlib
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .primary_control import commitment, unavailable_admission, check, _assign_validated
from .primary_selection_schema import ContactSelection
from .identity_sync import canonical

def lock_scope(c):
    return c.execute('SELECT * FROM shadify_app_admin WHERE singleton=true FOR UPDATE').fetchone()

def latest(c):
    return c.execute('SELECT * FROM shadify_primary_selections ORDER BY selection_revision DESC LIMIT 1').fetchone()

def summary(row,current):
    return dict(operation_id=str(row['operation_id']), state=row['state'], selection_revision=row['selection_revision'],
        current=current, email_masked=row['email_masked'], mobile_masked=row['phone_masked'],
        assigned_id=str(row['assigned_id']) if row['assigned_id'] else None, assigned_revision=row['assigned_revision'])

def observe_selection(connection,*,change:ContactSelection,admission=unavailable_admission):
    check(admission,change,'result')
    with connection.cursor(row_factory=dict_row) as c:
        state=lock_scope(c)
        receipt=c.execute('SELECT * FROM shadify_primary_operations WHERE operation_id=%s',(change.operation_id,)).fetchone()
        if receipt is None:
            check(admission,change,'result')
            return {'state':'unknown'}
        if receipt['request_sha256']!=commitment(change):raise HTTPException(409,'primary_operation_conflict')
        row=c.execute('SELECT * FROM shadify_primary_selections WHERE operation_id=%s',(change.operation_id,)).fetchone()
        if row is None:raise HTTPException(409,'primary_operation_conflict')
        current=latest(c)
        check(admission,change,'result')
        return dict(receipt=receipt['result'],selection=effective_summary(row,current['operation_id']==row['operation_id'],state),
            primary_id=str(state['primary_user_id']) if state['primary_user_id'] else None,primary_revision=state['revision'])

def select_contact(connection,*,change:ContactSelection,admission=unavailable_admission):
    kind,fingerprint=check(admission,change,'apply')
    with connection.cursor(row_factory=dict_row) as c:
        state=lock_scope(c)
        check(admission,change,'apply')
        if state is None:raise HTTPException(503,'app_admin_scope_unavailable')
        receipt=c.execute('SELECT * FROM shadify_primary_operations WHERE operation_id=%s',(change.operation_id,)).fetchone()
        if receipt:return observe_selection(connection,change=change,admission=admission)
        old=latest(c)
        if old and (old['node_id'],old['instance_id'],old['factor_scope_id'])!=(change.node_id,change.instance_id,str(change.factor_scope_id)):
            raise HTTPException(409,'primary_selection_binding_changed')
        if (state['revision']!=change.expected_primary_revision or state['primary_user_id']!=change.expected_primary_id
                or (old['selection_revision'] if old else 0)!=change.expected_selection_revision):
            raise HTTPException(409,'primary_revision_changed')
        if old and old['state'] in ('pending','ambiguous'):
            c.execute("UPDATE shadify_primary_selections SET state='superseded' WHERE operation_id=%s",(old['operation_id'],))
        def digest(kind,value):
            return hashlib.sha256(canonical([kind,change.domain_app,str(change.factor_scope_id),value])).hexdigest() if value else None
        receipt=dict(state='accepted',operation_id=str(change.operation_id),selection_revision=change.expected_selection_revision+1)
        c.execute('INSERT INTO shadify_primary_operations(operation_id,request_sha256,authority_sha256,authority_kind,result) VALUES (%s,%s,%s,%s,%s)',
            (change.operation_id,commitment(change),fingerprint,kind,Jsonb(receipt)))
        c.execute('INSERT INTO shadify_primary_selections(operation_id,domain_app,selection_revision,node_id,instance_id,factor_scope_id,'
            'expected_primary_id,expected_primary_revision,email_digest,phone_digest,email_masked,phone_masked,state) '
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')",
            (change.operation_id,change.domain_app,change.expected_selection_revision+1,change.node_id,change.instance_id,str(change.factor_scope_id),
             change.expected_primary_id,change.expected_primary_revision,digest('email',change.email),digest('phone',change.mobile),
             change.email[0]+'***@'+change.email.split('@')[1] if change.email else None,
             '+'+'*'*(len(change.mobile)-5)+change.mobile[-4:] if change.mobile else None))
        reconcile(c, factor_scope_id=str(change.factor_scope_id), state=state, installation=(str(change.node_id),str(change.instance_id)))
        check(admission,change,'apply')
        return observe_selection(connection,change=change,admission=admission)

def reconcile(c,*,factor_scope_id=None,state=None,installation=None):
    # Only called inside app-owned validated identity transactions. No expiry replay.
    state=state if state is not None else lock_scope(c)
    row=latest(c)
    if not row or row['state'] not in ('pending','ambiguous'):return
    if factor_scope_id is None or row['factor_scope_id']!=str(factor_scope_id):return
    if installation!=(str(row['node_id']),str(row['instance_id'])):return
    if state['revision']!=row['expected_primary_revision'] or state['primary_user_id']!=row['expected_primary_id']:
        c.execute("UPDATE shadify_primary_selections SET state='authority_changed' WHERE operation_id=%s",(row['operation_id'],))
        return
    users=c.execute("SELECT u.* FROM shadify_users u JOIN auth_identity_projections p ON p.user_id=u.id "
        "WHERE u.active AND u.first_admitted_at IS NOT NULL AND u.admission_source='canonical_shell' "
        "AND p.identity_status='active' AND p.domain_app='shadify' AND p.factor_scope_id=%s "
        "AND (p.email_digest=%s OR p.phone_digest=%s) ORDER BY u.id LIMIT 2 FOR UPDATE OF u,p",
        (row['factor_scope_id'],row['email_digest'],row['phone_digest'])).fetchall()
    status='pending' if not users else 'ambiguous' if len(users)>1 else 'assigned'
    assigned_id=assigned_revision=None
    if len(users)==1:
        if state['revision']>=9223372036854775807:raise HTTPException(409,'primary_revision_exhausted')
        receipt=c.execute('SELECT * FROM shadify_primary_operations WHERE operation_id=%s',(row['operation_id'],)).fetchone()
        result=_assign_validated(c,state,users[0],receipt['authority_kind'],receipt['authority_sha256'],row['operation_id'])
        assigned_id=users[0]['id'];assigned_revision=result['revision']
    c.execute('UPDATE shadify_primary_selections SET state=%s,assigned_id=%s,assigned_revision=%s WHERE operation_id=%s',
        (status,assigned_id,assigned_revision,row['operation_id']))


def read_state(connection, *, request, admission=unavailable_admission):
    from .primary_selection_schema import SelectionRead
    if not isinstance(request,SelectionRead):raise HTTPException(422,'primary_read_invalid')
    check(admission,request,'read')
    with connection.cursor(row_factory=dict_row) as c:
        state=lock_scope(c)
        if state is None:raise HTTPException(503,'app_admin_scope_unavailable')
        row=latest(c)
        if row and (row['node_id'],row['instance_id'],row['factor_scope_id'])!=(request.node_id,request.instance_id,str(request.factor_scope_id)):
            raise HTTPException(409,'primary_selection_binding_changed')
        check(admission,request,'read')
        return dict(state='absent' if row is None else effective_summary(row,True,state)['state'],
            primary_id=str(state['primary_user_id']) if state['primary_user_id'] else None,
            primary_revision=state['revision'],selection_revision=row['selection_revision'] if row else 0,
            selection=effective_summary(row,True,state) if row else None)


def effective_summary(row,current,state):
    value=summary(row,current)
    if row['state'] in ('pending','ambiguous') and (state['revision']!=row['expected_primary_revision'] or
            state['primary_user_id']!=row['expected_primary_id']):
        value['state']='authority_changed'
    return value
