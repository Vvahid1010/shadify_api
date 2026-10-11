"""One staged pipeline. Only verified outputs commit approval; no public bypass."""
import hashlib,os,time
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from uuid import uuid4,uuid5,UUID
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .ingest_worker import IngestWorker
from .ingest_scanner import fd_digest,InputChanged
from .storage import StorageUnavailable
from .media import Asset,MediaError
from .media_tools import ToolFailure,PIPELINE_VERSION
from .musicbrainz import ProviderUnavailable

class PipelineWorker(IngestWorker):
    def __init__(self,*,tools,provider,**kwargs):super().__init__(**kwargs);self.tools=tools;self.provider=provider
    def run_once(self):
        # Metadata traffic is drained before claiming any file attempt/lease.
        if hasattr(self.provider,'dispatch_once'):self.provider.dispatch_once()
        return super().run_once()
    def claim(self,connection):
        with connection.transaction():
            with connection.cursor(row_factory=dict_row) as c:
                job=c.execute("SELECT j.* FROM shadify_ingest_jobs j WHERE j.state NOT IN ('ready','rejected') "
                    "AND (j.last_error IS NULL OR j.updated_at<=clock_timestamp()-%s*interval '1 second' OR "
                    "(j.state='review_required' AND EXISTS(SELECT 1 FROM shadify_ingest_match_decisions d WHERE d.job_id=j.id))) "
                    "AND (j.state<>'review_required' OR EXISTS(SELECT 1 FROM shadify_ingest_match_decisions d WHERE d.job_id=j.id)) "
                    "AND (NOT EXISTS(SELECT 1 FROM shadify_ingest_attempts a WHERE a.job_id=j.id AND a.finished_at IS NULL) OR "
                    "EXISTS(SELECT 1 FROM shadify_ingest_attempts a WHERE a.job_id=j.id AND a.claimed_revision=j.revision AND a.finished_at IS NULL AND a.lease_expires_at<=clock_timestamp())) "
                    "AND (j.state<>'scanning' OR EXISTS(SELECT 1 FROM shadify_ingest_attempts a WHERE a.job_id=j.id AND a.claimed_revision=j.revision AND a.finished_at IS NULL)) "
                    'ORDER BY j.updated_at,j.created_at,j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1',(self.limits.retry_seconds,)).fetchone()
                if job is None:return None
                c.execute("UPDATE shadify_ingest_attempts SET finished_at=clock_timestamp(),outcome='abandoned' WHERE job_id=%s AND finished_at IS NULL",(job['id'],))
                state={'uploaded':'scanning','review_required':'processing'}.get(job['state'],job['state'])
                stage={'scanning':'scan','validating':'validation','matching':'matching','processing':'processing'}[state]
                job=c.execute("UPDATE shadify_ingest_jobs SET state=%s,revision=revision+1,last_error=NULL,updated_at=clock_timestamp() WHERE id=%s RETURNING *",(state,job['id'])).fetchone()
                attempt=c.execute("INSERT INTO shadify_ingest_attempts(id,job_id,input_sha256,claimed_revision,stage,lease_expires_at) VALUES(%s,%s,%s,%s,%s,clock_timestamp()+%s*interval '1 second') RETURNING *",(uuid4(),job['id'],job['input_sha256'],job['revision'],stage,self.limits.lease_seconds)).fetchone()
                return job,attempt
    def _scan(self,c,job):
        scan=c.execute("SELECT id FROM shadify_ingest_scans WHERE job_id=%s AND input_sha256=%s AND verdict='clean' AND full_coverage=true ORDER BY scanned_at DESC,id LIMIT 1",(job['id'],job['input_sha256'])).fetchone()
        if scan is None:raise ToolFailure('pipeline_evidence_invalid')
        return scan['id']
    def _stages(self,c,job):
        return {r['stage']:r for r in c.execute("SELECT * FROM shadify_ingest_stages WHERE job_id=%s",(job['id'],)).fetchall()}
    def _insert_stage(self,c,job,scan,stage,payload):
        return c.execute("INSERT INTO shadify_ingest_stages(id,job_id,input_sha256,scan_id,stage,payload) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",(uuid4(),job['id'],job['input_sha256'],scan,stage,Jsonb(payload))).fetchone()
    def _fenced(self,c,claim,asset,*,require_scan=True):
        job,attempt=claim
        # Scope existence reads need no artist UPDATE privilege. Promotion takes
        # implicit artist/track FK key-share locks via derived INSERT BEFORE job locks.
        if c.execute("SELECT id FROM shadify_artists WHERE id=%s",(asset.artist_id,)).fetchone() is None:raise MediaError(409,'Artist removed')
        if c.execute("SELECT id FROM shadify_tracks WHERE id=%s AND artist_id=%s",(asset.track_id,asset.artist_id)).fetchone() is None:raise MediaError(409,'Track removed')
        current=c.execute("SELECT * FROM shadify_ingest_jobs WHERE id=%s FOR UPDATE",(job['id'],)).fetchone()
        active=c.execute("SELECT * FROM shadify_ingest_attempts WHERE id=%s AND finished_at IS NULL AND lease_expires_at>clock_timestamp() FOR UPDATE",(attempt['id'],)).fetchone()
        if current['state']!=job['state'] or current['revision']!=job['revision'] or active is None:raise MediaError(409,'Pipeline attempt changed or expired')
        return self._scan(c,job) if require_scan else None
    def _finish(self,c,claim,state,error=None):
        job,attempt=claim
        # The initial fence may precede locks/triggers/evidence work. Guard the
        # final transition with database time too; failure rolls back ALL evidence,
        # derived rows and approvals in this transaction. Durable manifest/bytes
        # remain private and can be verified/reused by the next admitted attempt.
        active=c.execute("UPDATE shadify_ingest_attempts SET finished_at=clock_timestamp(),outcome='completed' WHERE id=%s AND job_id=%s AND claimed_revision=%s AND finished_at IS NULL AND lease_expires_at>clock_timestamp() RETURNING id",(attempt['id'],job['id'],job['revision'])).fetchone()
        if active is None:raise MediaError(409,'Pipeline attempt changed or expired')
        current=c.execute("UPDATE shadify_ingest_jobs SET state=%s,last_error=%s,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s AND state=%s AND revision=%s AND EXISTS(SELECT 1 FROM shadify_ingest_attempts a WHERE a.id=%s AND a.job_id=shadify_ingest_jobs.id AND a.claimed_revision=shadify_ingest_jobs.revision AND a.outcome='completed' AND a.lease_expires_at>clock_timestamp()) RETURNING id",(state,error,job['id'],job['state'],job['revision'],attempt['id'])).fetchone()
        if current is None:raise MediaError(409,'Pipeline attempt changed or expired')
        return dict(job_id=str(job['id']),state=state,revision=job['revision']+1,approved=state=='ready',retryable=error in {'provider_unavailable','processing_unavailable','processing_timeout','processing_error'},last_error=error)
    @contextmanager
    def _input(self,asset,job,deadline):
        adapter=self.adapters.get(asset.storage_provider)
        if adapter is None or asset.storage_provider!='local':raise ToolFailure('processing_unavailable')
        fd=adapter.open(asset.key)
        try:
            before=os.fstat(fd)
            if before.st_size!=asset.size or before.st_size>self.limits.max_input_bytes:raise ToolFailure('bytes_changed')
            if fd_digest(fd,deadline=deadline,size=asset.size)!=job['input_sha256']:raise ToolFailure('bytes_changed')
            yield fd
            after=os.fstat(fd)
            if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or fd_digest(fd,deadline=deadline,size=asset.size)!=job['input_sha256']:raise ToolFailure('bytes_changed')
        finally:os.close(fd)
    def _read_asset(self,connection,job):
        with connection.cursor(row_factory=dict_row) as c:
            row=c.execute("SELECT * FROM shadify_media_assets WHERE id=%s",(job['asset_id'],)).fetchone()
            if row is None:raise ToolFailure('bytes_changed')
            return Asset(**row)
    def process(self,connection,claim):
        job,attempt=claim
        if job['state']=='scanning':return super().process(connection,claim)
        asset=self._read_asset(connection,job);deadline=time.monotonic()+self.limits.wall_seconds
        try:
            with connection.cursor(row_factory=dict_row) as c:
                self._scan(c,job);stages=self._stages(c,job)
            if job['state']=='validating':
                with self._input(asset,job,deadline) as fd:validation,extraction=self.tools.validate(fd,asset.mime,deadline=deadline)
                with connection.transaction(),connection.cursor(row_factory=dict_row) as c:
                    scan=self._fenced(c,claim,asset)
                    self._insert_stage(c,job,scan,'validation',validation);self._insert_stage(c,job,scan,'extraction',extraction)
                    return self._finish(c,claim,'matching')
            if job['state']=='matching':return self._matching(connection,claim,asset,stages,deadline)
            if job['state']=='processing':return self._processing(connection,claim,asset,stages,deadline)
            raise ToolFailure('pipeline_evidence_invalid')
        except InputChanged:return self._error(connection,claim,asset,'bytes_changed')
        except StorageUnavailable:return self._error(connection,claim,asset,'processing_error')
        except ProviderUnavailable:return self._error(connection,claim,asset,'provider_unavailable')
        except TimeoutError:return self._error(connection,claim,asset,'processing_timeout')
        except ToolFailure as exc:return self._error(connection,claim,asset,exc.code)
    def _error(self,connection,claim,asset,error):
        terminal=error in {'validation_invalid','metadata_invalid','bytes_changed','output_invalid','pipeline_evidence_invalid'}
        with connection.transaction(),connection.cursor(row_factory=dict_row) as c:
            self._fenced(c,claim,asset,require_scan=False);return self._finish(c,claim,'rejected' if terminal else claim[0]['state'],error)
    def _matching(self,connection,claim,asset,stages,deadline):
        job,_=claim
        if 'extraction' not in stages:raise ToolFailure('pipeline_evidence_invalid')
        if asset.asset_type in ('audio_original','video_original'):
            metadata=dict(stages['extraction']['payload']['metadata'])
            with connection.cursor(row_factory=dict_row) as c:
                row=c.execute("SELECT t.title,a.name FROM shadify_tracks t JOIN shadify_artists a ON a.id=t.artist_id WHERE t.id=%s AND t.artist_id=%s",(asset.track_id,asset.artist_id)).fetchone()
                if row is None:raise ToolFailure('pipeline_evidence_invalid')
                metadata['title']=metadata.get('title') or row['title'];metadata['artist']=metadata.get('artist') or row['name']
            if self.provider is None:raise ProviderUnavailable()
            payload=self.provider.lookup(metadata,deadline=deadline)
            automatic=payload.get('automatic_mbid')
            audio_decision=None
        else:
            with connection.cursor(row_factory=dict_row) as c:
                audio_decision=c.execute("SELECT d.id FROM shadify_ingest_match_decisions d JOIN shadify_ingest_jobs j ON j.id=d.job_id JOIN shadify_media_assets a ON a.id=j.asset_id WHERE d.kind='audio' AND j.state='ready' AND a.artist_id=%s AND a.track_id=%s ORDER BY d.decided_at DESC,d.id LIMIT 1",(asset.artist_id,asset.track_id)).fetchone()
            if audio_decision is None:raise ProviderUnavailable()
            payload=dict(complete=True,provider='track_audio',audio_decision_id=str(audio_decision['id']));automatic=None
        with connection.transaction(),connection.cursor(row_factory=dict_row) as c:
            scan=self._fenced(c,claim,asset);stage=self._insert_stage(c,job,scan,'matching',payload)
            if automatic or audio_decision:
                c.execute("INSERT INTO shadify_ingest_match_decisions(id,job_id,input_sha256,matching_stage_id,kind,method,selected_recording_mbid,cover_audio_decision_id,corrected_metadata,reason) VALUES(%s,%s,%s,%s,%s,'automatic',%s,%s,%s,%s)",
                    (uuid4(),job['id'],job['input_sha256'],stage['id'],'cover' if audio_decision else 'audio',automatic,audio_decision['id'] if audio_decision else None,Jsonb({}),'Same-track ready audio decision' if audio_decision else 'Unique ISRC with exact title/artist and duration'))
                return self._finish(c,claim,'processing')
            return self._finish(c,claim,'review_required','match_ambiguous')
    def _processing(self,connection,claim,asset,stages,deadline):
        job,_=claim;adapter=self.adapters['local']
        if not {'validation','extraction','matching'}<=stages.keys():raise ToolFailure('pipeline_evidence_invalid')
        with connection.cursor(row_factory=dict_row) as c:
            decision=c.execute("SELECT * FROM shadify_ingest_match_decisions WHERE job_id=%s",(job['id'],)).fetchone()
        if decision is None:raise ToolFailure('pipeline_evidence_invalid')
        prepared=stages.get('prepared_outputs');manifest=prepared['payload'] if prepared else None
        if manifest and (manifest['pipeline_version']!=PIPELINE_VERSION or manifest['tool_identity']!=self.tools.identity):raise ToolFailure('pipeline_evidence_invalid')
        missing=set();existing={}
        if manifest:
            for role,output in manifest['outputs'].items():
                try:
                    fd=adapter.open(output['key'])
                except Exception:
                    # Distinguish absent keys from unreadable/symlink objects; no overwrite.
                    with adapter.parent(output['key'],create=True) as (parent,name):
                        try:os.stat(name,dir_fd=parent,follow_symlinks=False)
                        except FileNotFoundError:missing.add(role);continue
                    raise ToolFailure('output_invalid')
                try:
                    if os.fstat(fd).st_size!=output['size'] or fd_digest(fd,deadline=deadline,size=output['size'])!=output['sha256']:raise ToolFailure('output_invalid')
                finally:os.close(fd)
                existing[role]=output
        with self._input(asset,job,deadline) as fd:
            derived_missing=None if manifest is None else missing-{'master'}
            generated=self.tools.process(fd,asset.mime,stages['validation']['payload'],deadline=deadline,roles=derived_missing) if derived_missing is None or derived_missing else {}
            if manifest is None or 'master' in missing:
                os.lseek(fd,0,os.SEEK_SET);parts=[];read=0
                while block:=os.read(fd,min(1048576,asset.size+1-read)):
                    parts.append(block);read+=len(block)
                    if read>asset.size:raise ToolFailure('bytes_changed')
                data=b''.join(parts)
                if len(data)!=asset.size or hashlib.sha256(data).hexdigest()!=job['input_sha256']:raise ToolFailure('bytes_changed')
                generated['master']=(asset.mime,data)
            if manifest is None:
                outputs={}
                for role,(mime,data) in generated.items():
                    output_id=uuid5(job['id'],PIPELINE_VERSION+':'+role)
                    key=(f'artists/{asset.artist_id}/tracks/{asset.track_id}/{output_id}/master' if role=='master' else f'delivery/{output_id}/{role}')
                    outputs[role]=dict(asset_id=str(output_id),key=key,mime=mime,size=len(data),sha256=hashlib.sha256(data).hexdigest())
                required={'master','video'} if asset.asset_type=='video_original' else {'master','high','low'} if asset.asset_type=='audio_original' else {'master','cover'}
                if set(outputs)!=required:raise ToolFailure('output_invalid')
                manifest=dict(pipeline_version=PIPELINE_VERSION,tool_identity=self.tools.identity,outputs=outputs)
                with connection.transaction(),connection.cursor(row_factory=dict_row) as c:
                    scan=self._fenced(c,claim,asset);self._insert_stage(c,job,scan,'prepared_outputs',manifest)
            for role,(mime,data) in generated.items():
                output=manifest['outputs'][role]
                if len(data)!=output['size'] or hashlib.sha256(data).hexdigest()!=output['sha256']:raise ToolFailure('output_invalid')
                if time.monotonic()>=deadline:raise TimeoutError()
                try:
                    with adapter.stage(output['key'],mime) as staged:
                        view=memoryview(data)
                        while view:view=view[os.write(staged[0],view):]
                        adapter.install(staged)
                except FileExistsError:
                    check=adapter.open(output['key'])
                    try:
                        if fd_digest(check,deadline=deadline,size=output['size'])!=output['sha256']:raise ToolFailure('output_invalid')
                    finally:os.close(check)
        identities={}
        for role,output in manifest['outputs'].items():
            fd=adapter.open(output['key'])
            try:
                if fd_digest(fd,deadline=deadline,size=output['size'])!=output['sha256'] or adapter.inspect(output['key']).content_type!=output['mime']:raise ToolFailure('output_invalid')
                info=os.fstat(fd);identities[role]=dict(device=info.st_dev,inode=info.st_ino,size=info.st_size,mtime_ns=info.st_mtime_ns,ctime_ns=info.st_ctime_ns)
            finally:os.close(fd)
        if time.monotonic()>=deadline:raise TimeoutError()
        with connection.transaction(),connection.cursor(row_factory=dict_row) as c:
            targets={}
            for role,output in manifest['outputs'].items():
                master=role=='master';kind=asset.asset_type if master else 'audio_delivery' if role in ('high','low') else 'video_delivery' if role=='video' else 'artwork_delivery'
                target=Asset(UUID(output['asset_id']),asset.owner_id,asset.artist_id,asset.track_id,output['key'],kind,output['mime'],output['size'],datetime.now(timezone.utc)+timedelta(days=3650),'local','ready','private' if master else 'free','draft' if master else 'published')
                c.execute('INSERT INTO shadify_media_assets(id,owner_id,artist_id,track_id,key,asset_type,mime,size,expires_at,storage_provider,state,access_class,publish_state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',tuple(getattr(target,name) for name in Asset.__dataclass_fields__))
                targets[role]=target
            scan=self._fenced(c,claim,asset)
            if decision['kind']=='cover':
                ready=c.execute("SELECT j.id FROM shadify_ingest_jobs j JOIN shadify_ingest_match_decisions d ON d.job_id=j.id WHERE d.id=%s AND j.state='ready' FOR SHARE OF j",(decision['cover_audio_decision_id'],)).fetchone()
                if ready is None:raise ToolFailure('pipeline_evidence_invalid')
            self._insert_stage(c,job,scan,'processing',dict(complete=True,pipeline_version=PIPELINE_VERSION,tool_identity=self.tools.identity,identities=identities))
            for role,output in manifest['outputs'].items():
                target=targets[role]
                c.execute("INSERT INTO shadify_ingest_variants(job_id,role,asset_id,output_sha256,source_quality,pipeline_version) VALUES(%s,%s,%s,%s,%s,%s)",(job['id'],role,target.id,output['sha256'],Jsonb(stages['validation']['payload']['source_quality']),PIPELINE_VERSION))
                c.execute("INSERT INTO shadify_ingest_approvals(asset_id,job_id,scan_id,input_sha256,output_sha256,storage_key,storage_provider,mime,size,object_identity,validation,extraction,match_decision,processing) VALUES(%s,%s,%s,%s,%s,%s,'local',%s,%s,%s,%s,%s,%s,%s)",
                    (target.id,job['id'],scan,job['input_sha256'],output['sha256'],target.key,target.mime,target.size,Jsonb(identities[role]),Jsonb(stages['validation']['payload']),Jsonb(stages['extraction']['payload']),Jsonb(dict(decision=decision['method'],decision_id=str(decision['id']))),Jsonb(dict(complete=True,pipeline_version=PIPELINE_VERSION))))
            return self._finish(c,claim,'ready')
