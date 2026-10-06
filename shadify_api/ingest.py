"""Durable quarantine/approval reads; no public caller can approve an upload."""
import re
from uuid import uuid4
import psycopg
from psycopg.rows import dict_row
from .media import MediaError

INFRA_ERRORS={'scanner_unavailable','scanner_timeout','scanner_error','scanner_evidence_invalid'}

def digest(value):
    if type(value) is not str or re.fullmatch('[a-f0-9]{64}',value) is None:
        raise MediaError(503,'Immutable upload digest unavailable')
    return value

class IngestRepository:
    def enqueue_ingest(self,c,asset_id,sha256):
        sha256=digest(sha256)
        c.execute('INSERT INTO shadify_ingest_jobs(id,asset_id,input_sha256) VALUES (%s,%s,%s) '
                  'ON CONFLICT(asset_id) DO NOTHING',(uuid4(),asset_id,sha256))
        row=c.execute('SELECT input_sha256 FROM shadify_ingest_jobs WHERE asset_id=%s',(asset_id,)).fetchone()
        if row['input_sha256']!=sha256:raise MediaError(409,'Upload bytes changed')

    def ingest_status(self,c,asset_id):
        row=c.execute('SELECT j.id,j.state,j.revision,j.last_error,a.asset_id AS approved_asset '
                      'FROM shadify_ingest_jobs j LEFT JOIN shadify_ingest_approvals a '
                      'ON a.job_id=j.id AND a.asset_id=%s WHERE j.asset_id=%s OR a.asset_id=%s '
                      'ORDER BY a.asset_id NULLS LAST LIMIT 1',(asset_id,asset_id,asset_id)).fetchone()
        if row is None:
            return dict(job_id=None,state='uploaded',revision=None,approved=False,retryable=False,
                        last_error=None,legacy_unregistered=True)
        return dict(job_id=str(row['id']),state=row['state'],revision=row['revision'],
                    approved=row['state']=='ready' and row['approved_asset'] is not None,retryable=row['last_error'] in INFRA_ERRORS,
                    last_error=row['last_error'],legacy_unregistered=False)

    def require_ingest_approval(self,asset,*,cursor=None):
        if asset.key.startswith('quarantine/'):
            raise MediaError(409,'Media ingest approval required')
        def read(c):
            # A savepoint keeps a missing-schema/permission failure from poisoning
            # an outer artist read transaction; it never grants a compatibility bypass.
            with c.connection.transaction():
                return c.execute('SELECT a.object_identity FROM shadify_ingest_approvals a '
                    'JOIN shadify_ingest_jobs j ON j.id=a.job_id '
                    'JOIN shadify_media_assets source ON source.id=j.asset_id '
                    'JOIN shadify_ingest_scans s ON s.id=a.scan_id AND s.job_id=j.id '
                    "AND s.input_sha256=j.input_sha256 AND s.verdict='clean' "
                    "WHERE a.asset_id=%s AND j.state='ready' AND a.input_sha256=j.input_sha256 "
                    'AND source.artist_id=%s AND source.track_id=%s '
                    'AND a.storage_key=%s AND a.storage_provider=%s AND a.mime=%s AND a.size=%s' +
                    (' FOR SHARE OF j' if cursor is not None else ''),
                    (asset.id,asset.artist_id,asset.track_id,asset.key,asset.storage_provider,asset.mime,asset.size)).fetchone()
        try:
            if cursor is None:
                with self.connect() as connection:
                    with connection.cursor(row_factory=dict_row) as c:row=read(c)
            else:row=read(cursor)
        except psycopg.Error:
            raise MediaError(503,'Media ingest approval unavailable') from None
        if row is None:raise MediaError(409,'Media ingest approval required')
        identity=row['object_identity']
        keys={'device','inode','size','mtime_ns','ctime_ns'}
        if (type(identity) is not dict or set(identity)!=keys or
                any(type(v) is not int or v<0 for v in identity.values()) or identity['size']!=asset.size):
            raise MediaError(409,'Approved object evidence unavailable')
        return identity
