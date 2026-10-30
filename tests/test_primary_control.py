from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
import time
import psycopg
from psycopg.rows import dict_row
import pytest
from fastapi import HTTPException
from isolated_postgres import IsolatedPostgres
from shadify_api.primary_control import PrimaryChange,apply_primary,observe_result

def test_private_transaction_real_postgres():
    with IsolatedPostgres() as pg:
        with psycopg.connect(**pg.connection) as c:
            for migration in sorted(Path('migrations').glob('*.sql')):
                c.execute(migration.read_text())
            ids=[uuid4(),uuid4()];scope=uuid4()
            for uid in ids:
                c.execute("INSERT INTO shadify_users(id,auth_account_id,active,first_admitted_at,last_admitted_at,admission_source,name,bio) VALUES (%s,%s,true,now(),now(),'canonical_shell','Local name','Local bio')",(uid,str(uuid4())))
                c.execute("INSERT INTO auth_identity_projections(user_id,domain_app,factor_scope_id,version,payload_sha256,identity_status) VALUES (%s,'shadify',%s,7,%s,'active')",(uid,str(scope),'b'*64))
        request=PrimaryChange(operation_id=uuid4(),instance_id=uuid4(),node_id=uuid4(),domain_app='shadify',factor_scope_id=scope,target_user_id=ids[0],expected_primary_user_id=None,expected_revision=0,expires_at=int(time.time())+120)
        def admission(change,*,purpose):
            if change.instance_id!=request.instance_id or change.node_id!=request.node_id or (purpose=='apply' and change.expires_at<=time.time()):
                raise HTTPException(403,'synthetic_authority_denied')
            return 'dev_operator','a'*64
        def apply(change,reader=admission):
            with psycopg.connect(**pg.connection) as c:return apply_primary(c,change=change,admission=reader)
        with pytest.raises(HTTPException) as e:apply_primary(None,change=request)
        assert e.value.detail=='primary_control_authority_unavailable'
        for update in ({'factor_scope_id':uuid4()},{'instance_id':uuid4()},{'expires_at':1}):
            with pytest.raises(HTTPException):apply(request.model_copy(update=update))
        initial=apply(request)
        assert apply(request)==initial
        with psycopg.connect(**pg.connection) as c:
            c.execute("INSERT INTO shadify_app_admin_delegates(user_id,permissions) VALUES (%s,ARRAY['edit_profile'])",(ids[0],))
        next_request=request.model_copy(update={'operation_id':uuid4(),'target_user_id':ids[1],'expected_primary_user_id':ids[0],'expected_revision':1})
        def contend(change):
            try:return apply(change)
            except HTTPException as error:return error.detail
        rival=next_request.model_copy(update={'operation_id':uuid4()})
        with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(contend,(next_request,rival)))
        assert sum(isinstance(value,dict) for value in outcomes)==1 and 'primary_revision_changed' in outcomes
        winner=next_request if isinstance(outcomes[0],dict) else rival
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(apply,(winner,winner)))
        assert results[0]==results[1] and results[0]['revision']==2
        with pytest.raises(HTTPException) as e:apply(winner.model_copy(update={'expires_at':winner.expires_at+1}))
        assert e.value.detail=='primary_operation_conflict'
        failed=winner.model_copy(update={'operation_id':uuid4(),'target_user_id':ids[0],'expected_primary_user_id':ids[1],'expected_revision':2})
        calls=[]
        def late_denial(change,*,purpose):
            calls.append(purpose)
            if len(calls)==4:raise HTTPException(403,'synthetic_final_denial')
            return admission(change,purpose=purpose)
        with pytest.raises(HTTPException):apply(failed,late_denial)
        with psycopg.connect(**pg.connection,row_factory=dict_row) as c:
            assert observe_result(c,change=winner,admission=admission)==results[0]
            assert observe_result(c,change=failed,admission=admission)=={'state':'unknown'}
            assert c.execute('SELECT revision FROM shadify_app_admin').fetchone()['revision']==2
            assert c.execute('SELECT count(*) AS n FROM shadify_primary_operations').fetchone()['n']==2
            assert c.execute('SELECT count(*) AS n FROM shadify_app_admin_delegates').fetchone()['n']==1
            assert c.execute('SELECT count(*) AS n FROM shadify_app_admin_audit').fetchone()['n']==2
            for user in c.execute('SELECT * FROM shadify_users').fetchall():
                assert user['name']=='Local name' and user['bio']=='Local bio' and user['active']
            assert {row['version'] for row in c.execute('SELECT version FROM auth_identity_projections').fetchall()}=={7}
