"""Native Shadify scanner worker. No HTTP/Auth listener, installation or approval."""
import argparse,fcntl,os,signal,stat,threading,time
from ipaddress import ip_address
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from pydantic import BaseModel,ConfigDict,Field,model_validator
from psycopg.rows import dict_row
from .config import load_protected_json,load_database_config
from .local_storage import LocalAdapter
from .media import Asset,MediaError
from .ingest_scanner import scan_input,ScanVerdict
from .clamav_scanner import ClamAVScanner

LOCK_SQL="SELECT pg_try_advisory_lock(hashtextextended(current_database()||':'||'shadify_ingest_jobs'::regclass::oid::text||':shadify-ingest-worker',0))"

class WorkerLimits(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    max_input_bytes:int=Field(default=104857600,ge=1,le=104857600)
    max_scan_bytes:int=Field(default=209715200,ge=1,le=209715200)
    wall_seconds:int=Field(default=180,ge=1,le=180)
    cpu_seconds:int=Field(default=180,ge=1,le=180)
    memory_bytes:int=Field(default=2147483648,ge=268435456,le=2147483648)
    output_bytes:int=Field(default=32768,ge=1024,le=32768)
    lease_seconds:int=Field(default=360,ge=120,le=600)
    retry_seconds:int=Field(default=30,ge=1,le=300)
    poll_seconds:int=Field(default=5,ge=1,le=30)
    @model_validator(mode='after')
    def bounds(self):
        if self.max_scan_bytes<self.max_input_bytes or self.lease_seconds<self.wall_seconds+120:
            raise ValueError('Scan/lease bounds invalid')
        return self

class WorkerConfig(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,hide_input_in_errors=True)
    database_file:str
    local_root:str
    state_dir:str
    temp_dir:str
    clamscan:str
    prlimit:str
    definitions_dir:str
    engine_version:str=Field(pattern=r'^1\.4\.[0-9]{1,3}$')
    limits:WorkerLimits

def safe_path(path,*,directory=False,private=False,root_owned=False):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('Absolute protected path required')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for i,part in enumerate(path.parts[1:]):
            flags=os.O_RDONLY|os.O_NOFOLLOW
            if i<len(path.parts)-2 or directory:flags|=os.O_DIRECTORY
            child=os.open(part,flags,dir_fd=fd);os.close(fd);fd=child
        info=os.fstat(fd)
        if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or
            info.st_mode & (0o077 if private else 0o022) or
            (root_owned and info.st_uid!=0) or (private and info.st_uid not in (0,os.geteuid()))):
            raise ValueError('Protected path ownership/mode invalid')
    finally:os.close(fd)
    return path

@contextmanager
def host_lock(directory):
    directory=safe_path(directory,directory=True,private=True)
    parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:fd=os.open('worker.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=parent)
    finally:os.close(parent)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or info.st_mode&0o077:
            raise ValueError('Worker lock invalid')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Worker already running') from None
        yield fd # Scanner inherits this descriptor, retaining exclusion on parent death.
    finally:os.close(fd)

class IngestWorker:
    def __init__(self,*,connect,adapters,scanner,limits):
        self.connect=connect;self.adapters=adapters;self.scanner=scanner;self.limits=limits

    @contextmanager
    def session(self):
        with self.connect() as connection:
            connection.commit();connection.autocommit=True
            acquired=connection.execute(LOCK_SQL).fetchone()[0]
            yield connection if acquired else None
            # Closing the connection always releases this session's exclusion.

    def claim(self,connection):
        with connection.transaction():
            with connection.cursor(row_factory=dict_row) as c:
                job=c.execute("SELECT j.* FROM shadify_ingest_jobs j WHERE "
                    "(j.state='uploaded' AND (j.last_error IS NULL OR j.updated_at<=clock_timestamp()-%s*interval '1 second')) OR "
                    "(j.state='scanning' AND EXISTS(SELECT 1 FROM shadify_ingest_attempts a WHERE a.job_id=j.id "
                    "AND a.claimed_revision=j.revision AND a.finished_at IS NULL AND a.lease_expires_at<=clock_timestamp())) "
                    'ORDER BY j.created_at,j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1',(self.limits.retry_seconds,)).fetchone()
                if job is None:return None
                if job['state']=='scanning':
                    c.execute("UPDATE shadify_ingest_attempts SET finished_at=clock_timestamp(),outcome='abandoned' "
                              'WHERE job_id=%s AND claimed_revision=%s AND finished_at IS NULL',(job['id'],job['revision']))
                job=c.execute("UPDATE shadify_ingest_jobs SET state='scanning',revision=revision+1,last_error=NULL,"
                              'updated_at=clock_timestamp() WHERE id=%s RETURNING *',(job['id'],)).fetchone()
                attempt=c.execute('INSERT INTO shadify_ingest_attempts(id,job_id,input_sha256,claimed_revision,lease_expires_at) '
                    "VALUES (%s,%s,%s,%s,clock_timestamp()+%s*interval '1 second') RETURNING *",
                    (uuid4(),job['id'],job['input_sha256'],job['revision'],self.limits.lease_seconds)).fetchone()
                return job,attempt

    def finish(self,connection,claim,verdict):
        job,attempt=claim
        if type(verdict) is not ScanVerdict or verdict.sha256!=job['input_sha256']:raise MediaError(409,'Invalid attempt evidence')
        state='validating' if verdict.verdict=='clean' else ('rejected' if verdict.verdict=='infected' or verdict.error_code=='bytes_changed' else 'uploaded')
        error='malware_detected' if verdict.verdict=='infected' else verdict.error_code
        with connection.transaction():
            with connection.cursor(row_factory=dict_row) as c:
                current=c.execute('SELECT * FROM shadify_ingest_jobs WHERE id=%s FOR UPDATE',(job['id'],)).fetchone()
                active=c.execute('SELECT * FROM shadify_ingest_attempts WHERE id=%s AND finished_at IS NULL '
                    'AND lease_expires_at>clock_timestamp() FOR UPDATE',(attempt['id'],)).fetchone()
                if current['state']!='scanning' or current['revision']!=job['revision'] or active is None:
                    raise MediaError(409,'Attempt changed or lease expired; result not committed')
                c.execute('INSERT INTO shadify_ingest_scans(id,job_id,input_sha256,verdict,engine_version,definitions_version,error_code,full_coverage) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',(uuid4(),job['id'],job['input_sha256'],verdict.verdict,
                    verdict.engine_version,verdict.definitions_version,verdict.error_code,verdict.full_coverage))
                c.execute("UPDATE shadify_ingest_attempts SET finished_at=clock_timestamp(),outcome='completed' WHERE id=%s",(attempt['id'],))
                c.execute('UPDATE shadify_ingest_jobs SET state=%s,last_error=%s,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s',
                    (state,error,job['id']))
        return dict(job_id=str(job['id']),state=state,revision=job['revision']+1,approved=False,retryable=state=='uploaded',last_error=error)

    def process(self,connection,claim):
        job,_=claim
        with connection.cursor(row_factory=dict_row) as c:
            row=c.execute('SELECT * FROM shadify_media_assets WHERE id=%s',(job['asset_id'],)).fetchone()
        asset=Asset(**row)
        deadline=time.monotonic()+getattr(self.limits,'wall_seconds',180)
        verdict=scan_input(asset,job['input_sha256'],adapters=self.adapters,scanner=self.scanner,deadline=deadline,
            max_bytes=getattr(self.limits,'max_input_bytes',104857600)) if asset.state=='uploaded' else ScanVerdict('unknown',job['input_sha256'],error_code='bytes_changed')
        return self.finish(connection,claim,verdict)

    def run_once(self):
        with self.session() as connection:
            if connection is None:return None
            claim=self.claim(connection)
            return None if claim is None else self.process(connection,claim)

def database_address(database):
    return '127.0.0.1' if database.host=='localhost' else str(ip_address(database.host))

def compose(path):
    # No path discovery, service provision, migrations, roles or signature update.
    try:
        if os.geteuid()==0:raise ValueError('Worker must run as selected non-root app identity')
        config=WorkerConfig.model_validate(load_protected_json(Path(path)))
        safe_path(config.database_file,private=True)
        database=load_database_config(Path(config.database_file))
        database_address(database) # Reject DNS/multi-host lookups; one assigned numeric endpoint.
        safe_path(config.state_dir,directory=True,private=True)
        safe_path(config.temp_dir,directory=True,private=True)
        safe_path(config.clamscan,root_owned=True);safe_path(config.prlimit,root_owned=True)
        safe_path(config.definitions_dir,directory=True)
        if os.stat(config.definitions_dir).st_uid==os.geteuid() and os.geteuid()!=0:
            raise ValueError('Worker must not own writable definitions')
        adapter=LocalAdapter(Path(config.local_root))
        paths=[Path(config.local_root).resolve(),Path(config.temp_dir).resolve(),Path(config.state_dir).resolve(),Path(config.definitions_dir).resolve()]
        for i,left in enumerate(paths):
            for right in paths[i+1:]:
                if left.is_relative_to(right) or right.is_relative_to(left):raise ValueError('Worker roots must be separate')
        return config,database,adapter
    except Exception:raise RuntimeError('Worker protected configuration invalid; values redacted') from None

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config,database,adapter=compose(args.config)
    stopped=threading.Event()
    def stop(signum,frame):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    try:
        with host_lock(config.state_dir) as lock_fd:
            scanner=ClamAVScanner(executable=config.clamscan,prlimit=config.prlimit,definitions=config.definitions_dir,
                temp_dir=config.temp_dir,engine_version=config.engine_version,limits=config.limits,lock_fd=lock_fd)
            worker=IngestWorker(connect=lambda:database._connect(5,10000,5000,hostaddr=database_address(database)),adapters={'local':adapter},scanner=scanner,limits=config.limits)
            while not stopped.is_set():
                try:
                    result=worker.run_once()
                    if result is not None:print('ingest',result['job_id'],result['state'],result['revision'],flush=True)
                except KeyboardInterrupt:raise
                except Exception:print('ingest worker iteration unavailable; values redacted',flush=True)
                stopped.wait(config.limits.poll_seconds)
    except KeyboardInterrupt:return

if __name__=='__main__':main()
