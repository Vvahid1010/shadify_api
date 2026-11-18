"""Derive app-owned migration fingerprints in disposable PostgreSQL18, never a live DB."""
import json
import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

CREATE_ROLES = 'CREATE ROLE "Shadify_api_owner" LOGIN NOSUPERUSER NOCREATEDB\nNOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2;\nCREATE ROLE "Shadify_api" LOGIN NOSUPERUSER NOCREATEDB\nNOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10;'

CREATE_DATABASE = 'CREATE DATABASE shadify_api OWNER "Shadify_api_owner" TEMPLATE template0 ENCODING \'UTF8\''

DATABASE_ACCESS = 'REVOKE ALL ON DATABASE shadify_api FROM PUBLIC;\nGRANT CONNECT ON DATABASE shadify_api TO "Shadify_api";\nREVOKE ALL ON SCHEMA public FROM PUBLIC;\nALTER SCHEMA public OWNER TO "Shadify_api_owner";\nGRANT USAGE ON SCHEMA public TO "Shadify_api";'

INVENTORY = "SELECT json_build_object(\n 'database_owner',(SELECT pg_get_userbyid(datdba)\n    FROM pg_database WHERE datname=current_database()),\n 'schema_owner',(SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='public'),\n 'relations',COALESCE((SELECT json_agg(row_to_json(r) ORDER BY name) FROM (\n  SELECT c.relname AS name,c.relkind AS kind,pg_get_userbyid(c.relowner) AS owner,\n   c.relrowsecurity AS rls,c.relforcerowsecurity AS force_rls,\n   COALESCE((SELECT json_agg(row_to_json(a) ORDER BY position) FROM (\n    SELECT attnum AS position,attname AS name,format_type(atttypid,atttypmod) AS type,\n     attnotnull AS not_null,attidentity AS identity,attgenerated AS generated,\n     attisdropped AS dropped,pg_get_expr(d.adbin,d.adrelid) AS default_value\n    FROM pg_attribute LEFT JOIN pg_attrdef d ON d.adrelid=attrelid AND d.adnum=attnum\n    WHERE attrelid=c.oid AND attnum>0) a),'[]'::json) AS columns,\n   COALESCE((SELECT json_agg(row_to_json(k) ORDER BY name) FROM (\n    SELECT conname AS name,contype AS type,convalidated AS validated,\n     condeferrable AS deferrable,condeferred AS deferred,pg_get_constraintdef(oid,true)\n    AS definition\n    FROM pg_constraint WHERE conrelid=c.oid) k),'[]'::json) AS constraints,\n   COALESCE((SELECT json_agg(pg_get_indexdef(indexrelid) ORDER BY indexrelid::regclass::text)\n    FROM pg_index WHERE indrelid=c.oid),'[]'::json) AS indexes,\n   (SELECT count(*) FROM pg_trigger WHERE tgrelid=c.oid AND NOT tgisinternal) AS triggers,\n   (SELECT count(*) FROM pg_policy WHERE polrelid=c.oid) AS policies\n\n    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace\n    WHERE n.nspname='public') r),'[]'::json),\n 'functions',(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace\n  WHERE n.nspname='public'),\n 'types',(SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace\n  WHERE n.nspname='public' AND t.typrelid=0 AND t.typelem=0),\n 'operators',(SELECT count(*) FROM pg_operator o JOIN pg_namespace n ON n.oid=o.oprnamespace\n  WHERE n.nspname='public'))"


def generate(root, bindir=Path('/usr/lib/postgresql/18/bin')):
    def run(args, **kwargs):
        return subprocess.check_output([str(x) for x in args], stderr=subprocess.PIPE, **kwargs)
    with tempfile.TemporaryDirectory(prefix='shadify-metadata-') as temporary:
        directory=Path(temporary)
        run([bindir/'initdb','-D',directory/'data','-A','trust','--no-locale','-E','UTF8'])
        run([bindir/'pg_ctl','-D',directory/'data','-l',directory/'log','-o',f"-k {directory} -c listen_addresses=''",'-w','start'])
        try:
            def sql(statement, database='postgres', user=None):
                args=[bindir/'psql','-X','-qAt','-v','ON_ERROR_STOP=1','-h',directory,'-d',database]
                if user: args+=['-U',user]
                return run(args,input=statement.encode()).decode().strip()
            sql(CREATE_ROLES)
            sql(CREATE_DATABASE)
            sql(DATABASE_ACCESS,'shadify_api')
            steps=[]
            for path in sorted((root/'migrations').glob('*.sql')):
                if not re.fullmatch(r'[0-9]{3}_[a-z0-9_]+',path.stem): raise ValueError('invalid_migration_name')
                sql('SET search_path=public,pg_temp;\n'+path.read_text(),'shadify_api','Shadify_api_owner')
                inventory=json.loads(sql(INVENTORY,'shadify_api','Shadify_api_owner'))
                if sql("SELECT to_regclass('public.shadify_schema_revision') IS NOT NULL",'shadify_api','Shadify_api_owner')=='t':
                    inventory['app_revision']=sql('SELECT revision FROM public.shadify_schema_revision','shadify_api','Shadify_api_owner')
                digest=hashlib.sha256(json.dumps(inventory,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                steps.append({'target':path.stem,'after_sha256':digest})
            if not steps or len({s['after_sha256'] for s in steps})!=len(steps):
                raise ValueError('ambiguous_migration_state_requires_app_revision')
            return steps
        finally:
            run([bindir/'pg_ctl','-D',directory/'data','-m','fast','-w','stop'])

if __name__=='__main__':
    print(json.dumps(generate(Path(__file__).resolve().parents[1]),indent=2))
