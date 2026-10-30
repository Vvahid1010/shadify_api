from pathlib import Path
from uuid import uuid4, UUID
import time
import pytest
from fastapi import HTTPException
NODE=uuid4();INSTANCE=uuid4()

from concurrent.futures import ThreadPoolExecutor
import psycopg
from psycopg.rows import dict_row
from isolated_postgres import IsolatedPostgres
from shadify_api.primary_selection_schema import ContactSelection
from shadify_api.primary_selection import select_contact,observe_selection
from shadify_api.identity_sync_schema import IdentitySyncInput
from shadify_api.identity_sync import apply_projection
from shadify_api.access import ArtistAccessRepository
from shadify_api.auth import Identity
DOMAIN='shadify'

EXPIRED_OPERATIONS=set()

def synthetic_admission(change, *, purpose):
    # Synthetic fixture only. No installed dispatcher uses this callback.
    if purpose=='apply' and (change.expires_at<=time.time() or change.operation_id in EXPIRED_OPERATIONS):
        raise HTTPException(403,'expired')
    return 'dev_operator','a'*64

def request(scope, **updates):
    value=dict(operation_id=uuid4(),node_id=NODE,instance_id=INSTANCE,domain_app=DOMAIN,
        factor_scope_id=scope,email='selected@example.test',mobile=None,
        expected_primary_id=None,expected_primary_revision=0,expected_selection_revision=0,
        expires_at=int(time.time())+120)
    value.update(updates)
    return ContactSelection(**value)

@pytest.mark.parametrize('fields',[
    {'email':None,'mobile':None},{'email':'bad'}, {'mobile':'09123456789'},
    {'mobile':'+123'}, {'totp_verified':True}, {'approved':True},
    {'expected_primary_revision':True}, {'expected_selection_revision':-1}])
def test_invalid_selection(fields):
    with pytest.raises(ValueError):request(uuid4(),**fields)

@pytest.mark.parametrize('fields',[
    {'email':' UPPER@Example.test '}, {'email':None,'mobile':'+15555550123'},
    {'email':'selected@example.test','mobile':'+15555550123'}])
def test_normalization(fields):
    value=request(uuid4(),**fields)
    assert value.email is None or value.email==value.email.strip().lower()
    assert '@' not in repr(value) and '+15555550123' not in repr(value)

def test_real_pending_selection_lifecycle():
    with IsolatedPostgres() as pg:
        with psycopg.connect(**pg.connection) as c:
            for path in sorted(Path('migrations').glob('*.sql')):c.execute(path.read_text())
        scope=uuid4()
        def submit(value,admission=synthetic_admission):
            with psycopg.connect(**pg.connection) as c:return select_contact(c,change=value,admission=admission)
        def result(value):
            with psycopg.connect(**pg.connection) as c:return observe_selection(c,change=value,admission=synthetic_admission)
        def project(account,email=None,phone=None,version=1,status='active',pin=scope,installation=(str(NODE),str(INSTANCE))):
            value=IdentitySyncInput(schema_version=1,account_id=str(account),domain_app=DOMAIN,
                factor_scope_id=str(pin),proxy_app='shadify_api',version=version,identity_status=status,
                verified_email=email,verified_phone=phone)
            with psycopg.connect(**pg.connection) as c:return apply_projection(c,value,installation=installation)
        def admit(account,installation=(str(NODE),str(INSTANCE))):
            # Exercise actual canonical-observation hook with a synthetic fresh identity.
            repository=ArtistAccessRepository()
            repository.connect=lambda: psycopg.connect(**pg.connection,row_factory=dict_row)
            repository.observe_identity(Identity(account_id=str(account),session_id=str(uuid4()),auth_level='otp',issued_at=int(time.time()),expires_at=int(time.time())+120,domain_app=DOMAIN,proxy_app='shadify_api'),installation=installation,factor_scope_id=str(scope),verify_installation=lambda:(installation,str(scope)))
        def count(table):
            with psycopg.connect(**pg.connection) as c:return c.execute('SELECT count(*) FROM '+table).fetchone()[0]
        first=uuid4();second=uuid4();third=uuid4()
        initial=request(scope,expires_at=int(time.time())+120)
        with pytest.raises(HTTPException) as e:select_contact(None,change=initial)
        assert e.value.detail=='primary_control_authority_unavailable'
        accepted=submit(initial)
        assert accepted['selection']['state']=='pending' and count('shadify_users')==0
        assert submit(initial)==accepted
        with pytest.raises(HTTPException):submit(initial.model_copy(update={'email':'different@example.test'}))
        # Verified projection alone must not manufacture canonical first admission.
        project(first,email=initial.email)
        assert result(initial)['selection']['state']=='pending'
        admit(first,installation=(str(uuid4()),str(INSTANCE)))
        assert result(initial)['selection']['state']=='pending'
        EXPIRED_OPERATIONS.add(initial.operation_id) # deterministic expired-admission fixture
        # Revocation after matching rolls back both first admission and assignment.
        calls=[]
        def revoked_after_wait():
            calls.append(1)
            if len(calls)==3:raise HTTPException(403,'synthetic_policy_revoked')
            return ((str(NODE),str(INSTANCE)),str(scope))
        repository=ArtistAccessRepository()
        repository.connect=lambda:psycopg.connect(**pg.connection,row_factory=dict_row)
        actor=Identity(account_id=str(first),session_id=str(uuid4()),auth_level='otp',issued_at=int(time.time()),
            expires_at=int(time.time())+120,domain_app=DOMAIN,proxy_app='shadify_api')
        with pytest.raises(HTTPException):
            repository.observe_identity(actor,installation=(str(NODE),str(INSTANCE)),factor_scope_id=str(scope),
                verify_installation=revoked_after_wait)
        assert result(initial)['selection']['state']=='pending' and count('shadify_app_admin_audit')==0
        admit(first)
        done=result(initial)
        assert done['selection']['state']=='assigned' and done['primary_revision']==1
        uid=done['primary_id']
        assert count('shadify_app_admin_audit')==1
        with pytest.raises(HTTPException):submit(initial) # admission expiry, not pending lifetime
        # Lost delivery response/repeated projection cannot duplicate assignment.
        project(first,email=initial.email)
        assert count('shadify_app_admin_audit')==1
        with psycopg.connect(**pg.connection) as c:
            c.execute("UPDATE shadify_users SET name='Local name',bio='Local bio' WHERE id=%s",(uid,))
            c.execute("INSERT INTO shadify_app_admin_delegates(user_id,permissions) VALUES (%s,ARRAY['edit_profile'])",(uid,))
        # Consumed contact reused by another account must never transfer.
        project(first,version=2)
        project(second,email=initial.email);admit(second)
        assert result(initial)['primary_id']==uid and count('shadify_app_admin_audit')==1
        # Existing two different accounts across OR selectors is ambiguous.
        project(first,phone='+15555550123',version=3)
        ambiguous=request(scope,email=initial.email,mobile='+15555550123',expected_primary_id=UUID(uid),
            expected_primary_revision=1,expected_selection_revision=1)
        assert submit(ambiguous)['selection']['state']=='ambiguous'
        assert result(ambiguous)['primary_id']==uid
        # Supersede pending, old contact arrival cannot resolve it.
        pending=request(scope,email='future@example.test',expected_primary_id=UUID(uid),
            expected_primary_revision=1,expected_selection_revision=2)
        assert submit(pending)['selection']['state']=='pending'
        assert result(ambiguous)['selection']['state']=='superseded'
        assert not result(ambiguous)['selection']['current']
        project(second,email=initial.email,version=2)
        assert result(pending)['selection']['state']=='pending'
        project(third,email='future@example.test',pin=uuid4());admit(third)
        assert result(pending)['selection']['state']=='pending'
        project(third,email='future@example.test',status='disabled')
        assert result(pending)['selection']['state']=='pending'
        with psycopg.connect(**pg.connection) as c:
            c.execute('UPDATE shadify_users SET active=false WHERE auth_account_id=%s',(str(third),))
        project(third,email='future@example.test',version=2)
        assert result(pending)['selection']['state']=='pending'
        with psycopg.connect(**pg.connection) as c:
            c.execute('UPDATE shadify_users SET active=true WHERE auth_account_id=%s',(str(third),))
        project(third,email='future@example.test',version=2)
        new=result(pending)
        assert new['selection']['state']=='assigned' and new['primary_id']!=uid
        # Stale signed data cannot resurrect an old selection or change current contact.
        project(third,email='stale@example.test',version=1)
        assert result(pending)['primary_id']==new['primary_id']
        # Both selectors, same account -> one assignment and same stable UUID.
        project(third,email='same@example.test',phone='+15555550124',version=3)
        same=request(scope,email='same@example.test',mobile='+15555550124',
            expected_primary_id=UUID(new['primary_id']),expected_primary_revision=2,expected_selection_revision=3)
        assert submit(same)['selection']['state']=='assigned'
        assert count('shadify_app_admin_audit')==3
        # Mobile-only; concurrency CAS one accepted operation, one conflict.
        mobile=request(scope,email=None,mobile='+15555550124',expected_primary_id=UUID(new['primary_id']),
            expected_primary_revision=3,expected_selection_revision=4)
        rival=mobile.model_copy(update={'operation_id':uuid4()})
        def compete(value):
            try:return submit(value)
            except HTTPException as error:return error.detail
        with ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(compete,(mobile,rival)))
        assert sum(isinstance(v,dict) for v in out)==1 and 'primary_revision_changed' in out
        winner=mobile if isinstance(out[0],dict) else rival
        assert submit(winner)==result(winner)
        from shadify_api.primary_selection_schema import SelectionRead
        from shadify_api.primary_selection import read_state
        read=SelectionRead(operation_id=uuid4(),node_id=NODE,instance_id=INSTANCE,domain_app=DOMAIN,
            factor_scope_id=scope,expires_at=int(time.time())+120)
        with psycopg.connect(**pg.connection) as c:
            assert read_state(c,request=read,admission=synthetic_admission)['state']=='assigned'
        with pytest.raises(HTTPException):
            with psycopg.connect(**pg.connection) as c:
                read_state(c,request=read.model_copy(update={'instance_id':uuid4()}),admission=synthetic_admission)
        with psycopg.connect(**pg.connection,row_factory=dict_row) as c:
            assert c.execute('SELECT count(*) n FROM shadify_app_admin_delegates').fetchone()['n']==1
            user=c.execute('SELECT * FROM shadify_users WHERE id=%s',(uid,)).fetchone()
            assert user['name']=='Local name' and user['bio']=='Local bio'
            assert c.execute('SELECT count(*) n FROM shadify_app_admin_audit').fetchone()['n']==4
            # Storage never contains raw selectors, proof or arbitrary lookup contact.
            row=c.execute('SELECT * FROM shadify_primary_selections WHERE operation_id=%s',(winner.operation_id,)).fetchone()
            assert winner.mobile not in str(row)

        race=request(scope,email='race-a@example.test',mobile='+15555550125',
            expected_primary_id=UUID(new['primary_id']),expected_primary_revision=4,expected_selection_revision=5)
        assert submit(race)['selection']['state']=='pending'
        a,b=uuid4(),uuid4();admit(a);admit(b)
        def arrival(value):
            if value==a:return project(a,email='race-a@example.test')
            return project(b,phone='+15555550125')
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(arrival,(a,b)))
        raced=result(race);stable=raced['primary_id']
        assert raced['selection']['state']=='assigned' and raced['primary_revision']==5
        project(a,email='race-a@example.test',version=2);project(b,phone='+15555550125',version=2)
        assert result(race)['primary_id']==stable
        waiting=request(scope,email='never@example.test',expected_primary_id=UUID(stable),
            expected_primary_revision=5,expected_selection_revision=6)
        submit(waiting)
        with psycopg.connect(**pg.connection) as c:c.execute('UPDATE shadify_app_admin SET revision=revision+1')
        assert result(waiting)['selection']['state']=='authority_changed'
