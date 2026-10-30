import concurrent.futures
from pathlib import Path
from uuid import uuid4
import psycopg
from psycopg.rows import dict_row
import pytest
from fastapi import HTTPException
from isolated_postgres import IsolatedPostgres
from shadify_api.identity_sync import apply_projection
from shadify_api.identity_sync_schema import IdentitySyncInput
from test_identity_projection import DATA

def test_postgres_versions_roles_provenance_and_rollback():
    with IsolatedPostgres() as pg:
        with psycopg.connect(**pg.connection) as c:
            c.execute("CREATE TABLE shadify_users(id uuid PRIMARY KEY,auth_account_id text UNIQUE NOT NULL,active boolean NOT NULL DEFAULT true,first_admitted_at timestamptz,last_admitted_at timestamptz,admission_source text,name text DEFAULT '',bio text DEFAULT '')")
            c.execute(Path('migrations/023_identity_projection.sql').read_text())
            identifier=uuid4()
            c.execute("INSERT INTO shadify_users(id,auth_account_id,active,name,bio) VALUES (%s,%s,false,'Local artist','Preserved')",(identifier,DATA['account_id']))
        def apply(version,**fields):
            with psycopg.connect(**pg.connection) as c:return apply_projection(c,IdentitySyncInput(**(DATA|{'version':version}|fields)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            receipts=list(pool.map(apply,[2,2,1]))
        assert receipts[0]==receipts[1]
        with psycopg.connect(**pg.connection,row_factory=dict_row) as c:
            row=c.execute('SELECT * FROM auth_identity_projections').fetchone()
            assert row['version']==2
            user=c.execute('SELECT * FROM shadify_users').fetchone()
            assert user['id']==identifier and user['active'] is False and user['name']=='Local artist' and user['bio']=='Preserved'
            assert user['first_admitted_at'] is None and user['admission_source'] is None
        with pytest.raises(HTTPException):apply(2,verified_email='different@example.invalid')
        apply(3,verified_email=None,verified_phone=None)
        apply(2)
        with psycopg.connect(**pg.connection,row_factory=dict_row) as c:
            row=c.execute('SELECT * FROM auth_identity_projections').fetchone()
            assert row['version']==3 and row['email_digest'] is None and row['phone_digest'] is None
        with pytest.raises(RuntimeError):
            with psycopg.connect(**pg.connection) as c:
                apply_projection(c,IdentitySyncInput(**(DATA|{'version':4,'identity_status':'blocked'})))
                raise RuntimeError('synthetic post-validation failure rolls back')
        with psycopg.connect(**pg.connection,row_factory=dict_row) as c:assert c.execute('SELECT version FROM auth_identity_projections').fetchone()['version']==3
        apply(4,identity_status='deleted')
        with psycopg.connect(**pg.connection,row_factory=dict_row) as c:
            row=c.execute('SELECT * FROM auth_identity_projections').fetchone()
            assert row['identity_status']=='deleted' and row['email_digest'] is None
