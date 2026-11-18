"""Internal scanner interface only. No installer, CLI, approval or HTTP grant."""
from dataclasses import dataclass
import hashlib,os,re,time
from uuid import UUID,uuid4
from typing import Protocol
import psycopg
from psycopg.rows import dict_row
from .media import Asset,MediaError
from .local_storage import LocalAdapter

@dataclass(frozen=True)
class ScanVerdict:
    verdict:str
    sha256:str
    engine_version:str|None=None
    definitions_version:str|None=None
    error_code:str|None=None
    full_coverage:bool=False

    def __post_init__(self):
        if self.verdict not in ('clean','infected','unknown') or type(self.sha256) is not str or re.fullmatch('[a-f0-9]{64}',self.sha256) is None:
            raise ValueError('Invalid scanner evidence')
        if self.verdict=='unknown':
            if self.error_code not in ('scanner_unavailable','scanner_timeout','scanner_error',
                                       'scanner_evidence_invalid','bytes_changed'):raise ValueError('Invalid scanner failure')
        elif (self.full_coverage is not True or self.error_code is not None or any(type(v) is not str or not 1<=len(v)<=256
                for v in (self.engine_version,self.definitions_version))):raise ValueError('Scanner versions required')

class Scanner(Protocol):
    """Bound execution and scan the entire immutable input, rejecting skipped,
    partial, size/time-limited or unknown coverage. Exit zero alone is not clean.
    An adapter must raise TimeoutError on its enforced deadline. No adapter or
    subprocess runner is shipped here; full_coverage is trusted adapter evidence.
    """
    def scan(self,fd:int,*,sha256:str)->ScanVerdict: ...

class InputChanged(Exception):pass

def fd_digest(fd,*,size=None,deadline=None):
    os.lseek(fd,0,os.SEEK_SET);hash_=hashlib.sha256()
    count=0
    while True:
        if deadline is not None and time.monotonic()>=deadline:raise TimeoutError('Input hashing deadline')
        block=os.read(fd,1024*1024 if size is None else min(1024*1024,size-count+1))
        if not block:break
        count+=len(block)
        if size is not None and count>size:raise InputChanged('Input grew')
        hash_.update(block)
    os.lseek(fd,0,os.SEEK_SET)
    return hash_.hexdigest()

class IngestScanner:
    """Trusted explicit composition. Clean advances only to validating."""
    def __init__(self,*,connect,adapters,scanner:Scanner|None=None):
        self.connect=connect;self.adapters=dict(adapters);self.scanner=scanner

    def scan_job(self,job_id,*,expected_revision):
        if type(expected_revision) is not int or expected_revision<1:raise MediaError(422,'Job revision required')
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                job=c.execute('SELECT * FROM shadify_ingest_jobs WHERE id=%s FOR UPDATE',(UUID(str(job_id)),)).fetchone()
                if job is None:raise MediaError(404,'Ingest job not found')
                if job['state']!='uploaded' or job['revision']!=expected_revision:
                    raise MediaError(409,'Ingest job changed')
                asset=Asset(**c.execute('SELECT * FROM shadify_media_assets WHERE id=%s',(job['asset_id'],)).fetchone())
                if asset.state!='uploaded':raise MediaError(409,'Upload not complete')
                c.execute("UPDATE shadify_ingest_jobs SET state='scanning',last_error=NULL,revision=revision+1,"
                          'updated_at=clock_timestamp() WHERE id=%s',(job['id'],))
        expected=job['input_sha256']
        verdict=scan_input(asset,expected,adapters=self.adapters,scanner=self.scanner)
        state='validating' if verdict.verdict=='clean' else ('rejected' if verdict.verdict=='infected'
              or verdict.error_code=='bytes_changed' else 'uploaded')
        error='malware_detected' if verdict.verdict=='infected' else verdict.error_code
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                current=c.execute('SELECT * FROM shadify_ingest_jobs WHERE id=%s FOR UPDATE',(job['id'],)).fetchone()
                if current['revision']!=job['revision']+1 or current['state']!='scanning':
                    raise MediaError(409,'Scanner job changed; result not committed')
                c.execute('INSERT INTO shadify_ingest_scans(id,job_id,input_sha256,verdict,engine_version,definitions_version,error_code,full_coverage) '
                          'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',(uuid4(),job['id'],expected,verdict.verdict,
                          verdict.engine_version,verdict.definitions_version,verdict.error_code,verdict.full_coverage))
                c.execute('UPDATE shadify_ingest_jobs SET state=%s,last_error=%s,revision=revision+1,updated_at=clock_timestamp() '
                          'WHERE id=%s',(state,error,job['id']))
        return dict(job_id=str(job['id']),state=state,revision=job['revision']+2,
                    approved=False,retryable=state=='uploaded',last_error=error)

def scan_input(asset,expected,*,adapters,scanner,deadline=None,max_bytes=104857600):
    if asset.size<=0 or asset.size>max_bytes:
        return ScanVerdict("unknown",expected,error_code="scanner_evidence_invalid")
    hash_deadline=deadline if deadline is not None else time.monotonic()+180
    verdict=ScanVerdict('unknown',expected,error_code='scanner_unavailable')
    adapter=adapters.get(asset.storage_provider)
    if scanner is not None and isinstance(adapter,LocalAdapter):
        fd=None
        try:
            fd=adapter.open(asset.key)
            identity=os.fstat(fd)
            fingerprint=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
            if identity.st_size!=asset.size or fd_digest(fd,size=asset.size,deadline=hash_deadline)!=expected:
                verdict=ScanVerdict('unknown',expected,error_code='bytes_changed')
            else:
                verdict=scanner.scan(fd,sha256=expected) if deadline is None else scanner.scan(fd,sha256=expected,deadline=deadline)
                if type(verdict) is not ScanVerdict or verdict.sha256!=expected:
                    verdict=ScanVerdict('unknown',expected,error_code='scanner_evidence_invalid')
                elif fd_digest(fd,size=asset.size,deadline=hash_deadline)!=expected or fingerprint(os.fstat(fd))!=fingerprint(identity):
                    verdict=ScanVerdict('unknown',expected,error_code='bytes_changed')
        except InputChanged:verdict=ScanVerdict('unknown',expected,error_code='bytes_changed')
        except TimeoutError:verdict=ScanVerdict('unknown',expected,error_code='scanner_timeout')
        except Exception:verdict=ScanVerdict('unknown',expected,error_code='scanner_error')
        finally:
            if fd is not None:os.close(fd)
    return verdict
