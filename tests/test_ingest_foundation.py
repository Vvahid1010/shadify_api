"""Real SQL/HTTP guards and synthetic scanner-interface decisions, NOT AV proof."""
import base64,hashlib,importlib.util,io,os,tempfile,wave,time
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
from threading import Event
from uuid import UUID,uuid4
import psycopg
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from authority_fixture import admitted_actor
from test_artist_postgres import ArtistPostgresCase
from shadify_api.domain import DomainService
from shadify_api.main import create_app
from shadify_api.media import Asset,MediaError,MediaService
from shadify_api.media_routes import create_media_app
from shadify_api.local_storage import LocalAdapter
from shadify_api.repository import PostgresMediaRepository
from shadify_api.storage_service import StorageService
from shadify_api.ingest_scanner import IngestScanner,ScanVerdict

OLD_SOURCE=Path(__file__).parent/'fixtures/storage_service_7665ec01.py'
spec=importlib.util.spec_from_file_location('shadify_api.historical_ingest_test_storage',OLD_SOURCE)
old_storage=importlib.util.module_from_spec(spec);spec.loader.exec_module(old_storage)
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM6sAAAAASUVORK5CYII=')

class FixtureScanner:
    """Safe synthetic decisions; not a ClamAV implementation/result claim."""
    def __init__(self,verdict='clean',error=None,bad_hash=False):
        self.verdict=verdict;self.error=error;self.bad_hash=bad_hash
    def scan(self,fd,*,sha256,deadline=None):
        if self.error:raise self.error
        data=b''
        while part:=os.read(fd,4096):data+=part
        assert hashlib.sha256(data).hexdigest()==sha256
        return ScanVerdict(self.verdict,'0'*64 if self.bad_hash else sha256,
                           'SYNTHETIC-engine-1','SYNTHETIC-safe-definitions-1',full_coverage=True)

class IngestFoundationTests(ArtistPostgresCase):
    def setUp(self):
        super().setUp();self.assign()
        self.directory=tempfile.TemporaryDirectory(prefix='shadify-ingest-proof-')
        self.adapter=LocalAdapter(Path(self.directory.name));self.repo=PostgresMediaRepository(self.connect)
        self.storage=StorageService(self.repo,{'local':self.adapter},'local','https://testserver',b'SYNTHETIC-MEDIA-KEY-NOT-LIVE-00000',access_ttl=3600)
        self.service=MediaService(self.repo,self.storage,1000000,300);self.actor='owner-a'
        self.api=TestClient(create_app(self.service,lambda:admitted_actor(self.actor),repository=self.repo,domain=DomainService(self.repo)))
        self.bytes=TestClient(create_media_app(self.storage))
        self.track=UUID(self.api.post('/api/artists/'+str(self.artist)+'/tracks',json={'title':'Quarantine proof'}).json()['track_id'])
        stream=io.BytesIO()
        with wave.open(stream,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000);wav.writeframes(b'\0'*32)
        self.audio=stream.getvalue()
        # A real historical row and capability, minted by exact accepted7665 source.
        self.legacy=self.asset(self.audio,'audio_original','audio/wav','uploaded','private','draft')
        self.historical=old_storage.StorageService(self.repo,{'local':self.adapter},'local','https://testserver',b'SYNTHETIC-MEDIA-KEY-NOT-LIVE-00000',access_ttl=3600)
        self.old_url=self.historical.private_access(admitted_actor('owner-a'),self.legacy.id).url
        self.apply('007_admin_nomination.sql','008_nomination_control.sql','009_ingest_quarantine.sql')

    def tearDown(self):
        self.directory.cleanup();super().tearDown()

    def asset(self,body,kind,mime,state,access,publish):
        id_=uuid4();asset=Asset(id_,'owner-a',self.artist,self.track,'artists/'+str(self.artist)+'/tracks/'+str(self.track)+'/'+str(id_)+'/master',
             kind,mime,len(body),datetime.now(timezone.utc)+timedelta(minutes=5),storage_provider='local',state=state,access_class=access,publish_state=publish)
        with self.adapter.stage(asset.key,mime) as staged:os.write(staged[0],body);self.adapter.install(staged)
        self.repo.insert(admitted_actor('owner-a'),asset)
        return asset

    def upload(self,*,cover=False):
        body=PNG if cover else self.audio
        response=self.api.post('/api/media/uploads',json=dict(artist_id=str(self.artist),track_id=str(self.track),
            asset_type='artwork_original' if cover else 'audio_original',mime='image/png' if cover else 'audio/wav',size=len(body)))
        self.assertEqual(response.status_code,201);dto=response.json()
        self.assertEqual(self.bytes.put(dto['upload']['url'],content=body,headers=dto['upload']['headers']).status_code,201)
        complete=self.api.post('/api/media/uploads/'+dto['asset_id']+'/complete')
        self.assertEqual(complete.status_code,200,complete.text)
        return self.repo.get(UUID(dto['asset_id'])),complete.json(),dto

    def metadata(self,asset):return self.api.get('/api/media/'+str(asset.id)).json()
    def scanner(self,scanner=None):return IngestScanner(connect=self.connect,adapters={'local':self.adapter},scanner=scanner)
    def job(self,asset):return self.metadata(asset)['ingest']

    def approved_fixture(self,*,cover=False):
        # Structural gate fixture ONLY: no claim of actual scan/decode/processing.
        source,_,_=self.upload(cover=cover);job=self.job(source)
        body=PNG if cover else self.audio
        output=self.asset(body,'artwork_delivery' if cover else 'audio_delivery','image/png' if cover else 'audio/wav','ready','free','published')
        fd=self.adapter.open(output.key)
        try:
            stat=os.fstat(fd)
            identity=dict(device=stat.st_dev,inode=stat.st_ino,size=stat.st_size,mtime_ns=stat.st_mtime_ns,ctime_ns=stat.st_ctime_ns)
        finally:os.close(fd)
        scan=uuid4();sha=hashlib.sha256(body).hexdigest()
        with self.connect() as c:
            c.execute("INSERT INTO shadify_ingest_scans(id,job_id,input_sha256,verdict,engine_version,definitions_version,full_coverage) VALUES (%s,%s,%s,'clean','SYNTHETIC','SYNTHETIC',true)",(scan,UUID(job['job_id']),sha))
            c.execute("INSERT INTO shadify_ingest_approvals(asset_id,job_id,scan_id,input_sha256,output_sha256,storage_key,storage_provider,mime,size,object_identity,validation,extraction,match_decision,processing) VALUES (%s,%s,%s,%s,%s,%s,'local',%s,%s,%s,%s,%s,%s,%s)",
                (output.id,UUID(job['job_id']),scan,sha,sha,output.key,output.mime,output.size,Jsonb(identity),Jsonb({'passed':True,'fixture':'synthetic'}),Jsonb({'complete':True}),Jsonb({'decision':'reviewed','fixture':'synthetic'}),Jsonb({'complete':True})))
            c.execute("UPDATE shadify_ingest_jobs SET state='ready',revision=revision+1 WHERE id=%s",(UUID(job['job_id']),))
        return output,job

    def test_structural_approval_gate_allows_read_then_withdrawal_blocks_old_token(self):
        output,job=self.approved_fixture()
        issued=self.api.post('/api/media/'+str(output.id)+'/access')
        self.assertEqual(issued.status_code,200,issued.text);url=issued.json()['url']
        self.assertEqual(self.bytes.get(url).content,self.audio)
        self.assertEqual(self.bytes.head(url).status_code,200)
        self.assertEqual(self.bytes.get(url,headers={'Range':'bytes=0-7'}).content,self.audio[:8])
        with self.connect() as c:c.execute("UPDATE shadify_ingest_jobs SET state='rejected',revision=revision+1 WHERE id=%s",(UUID(job['job_id']),))
        self.assertEqual(self.bytes.get(url).status_code,409)
        self.assertEqual(self.api.post('/api/media/'+str(output.id)+'/access').status_code,409)

    def test_approved_descriptor_change_denies_and_publication_lock_serializes_withdrawal(self):
        output,job=self.approved_fixture();url=self.storage.private_access(admitted_actor('owner-a'),output.id).url
        with self.connect() as holder:
            holder.execute("SELECT 1") # Match the already-open artist publication transaction.
            with holder.cursor(row_factory=psycopg.rows.dict_row) as cursor:
                self.repo.require_ingest_approval(output,cursor=cursor,lock=True)
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    with self.connect() as writer:
                        writer.execute("SET LOCAL lock_timeout='100ms'")
                        writer.execute("UPDATE shadify_ingest_jobs SET state='rejected',revision=revision+1 WHERE id=%s",(UUID(job['job_id']),))
        path=Path(self.directory.name)/output.key
        with path.open('r+b') as stream:stream.write(b'BAD!')
        self.assertEqual(self.bytes.get(url).status_code,409)

    def publish_approved_track(self,*,cover=True):
        audio,audio_job=self.approved_fixture()
        image,image_job=self.approved_fixture(cover=True) if cover else (None,None)
        path='/api/artists/'+str(self.artist)
        fields={'delivery_asset_id':str(audio.id),'access_class':'free'}
        if image:fields['artwork_asset_id']=str(image.id)
        response=self.api.patch(path+'/tracks/'+str(self.track),json=fields)
        self.assertEqual(response.status_code,200,response.text)
        if image:
            response=self.api.patch(path,json={'avatar_asset_id':str(image.id),'header_asset_id':str(image.id)})
            self.assertEqual(response.status_code,200,response.text)
        response=self.api.put(path+'/tracks/'+str(self.track)+'/publication',json={'state':'published'})
        self.assertEqual(response.status_code,200,response.text)
        response=self.api.put(path+'/publication',json={'state':'published'})
        self.assertEqual(response.status_code,200,response.text)
        return audio,audio_job,image,image_job

    def test_approved_audio_and_cover_survive_actual_readonly_public_page(self):
        audio,_,image,_=self.publish_approved_track()
        response=self.api.get('/api/artists/'+str(self.artist)+'/public')
        self.assertEqual(response.status_code,200,response.text)
        dto=response.json();self.assertEqual(len(dto['content']['tracks']),1)
        track=dto['content']['tracks'][0]
        self.assertEqual(track['delivery_asset_id'],str(audio.id))
        self.assertEqual(track['artwork_asset_id'],str(image.id))
        self.assertEqual(dto['page']['avatar_asset_id'],str(image.id))
        self.assertEqual(dto['page']['header_asset_id'],str(image.id))

    def test_withdrawn_or_legacy_cover_blocks_track_publication_and_public_projection(self):
        audio,_,image,job=self.publish_approved_track()
        path='/api/artists/'+str(self.artist)
        with self.connect() as c:
            c.execute("UPDATE shadify_ingest_jobs SET state='rejected',revision=revision+1 WHERE id=%s",(UUID(job['job_id']),))
        self.assertEqual(self.api.get(path+'/public').json()['content']['tracks'],[])
        response=self.api.put(path+'/tracks/'+str(self.track)+'/publication',json={'state':'published'})
        self.assertEqual(response.status_code,409,response.text)
        legacy=self.asset(PNG,'artwork_delivery','image/png','ready','free','published')
        # Emulate a persisted pre009 reference without inventing approval.
        with self.connect() as c:c.execute('UPDATE shadify_tracks SET artwork_asset_id=%s WHERE id=%s',(legacy.id,self.track))
        self.assertEqual(self.api.get(path+'/public').json()['content']['tracks'],[])
        self.assertEqual(self.api.put(path+'/tracks/'+str(self.track)+'/publication',json={'state':'published'}).status_code,409)

    def test_adapter_cannot_claim_clean_with_unknown_or_partial_coverage(self):
        class PartialScanner:
            def scan(self,fd,*,sha256,deadline=None):
                return ScanVerdict('clean',sha256,'SYNTHETIC-engine','SYNTHETIC-definitions')
        asset,_,_=self.upload();job=self.job(asset)
        result=self.scanner(PartialScanner()).scan_job(job['job_id'],expected_revision=job['revision'])
        self.assertEqual(result['state'],'uploaded');self.assertFalse(result['approved'])
        self.assertEqual(result['last_error'],'scanner_error')

    def test_quarantine_completion_hash_and_response_loss_have_one_durable_job(self):
        asset,complete,dto=self.upload();job=complete['ingest']
        self.assertTrue(asset.key.startswith('quarantine/'));self.assertEqual(job['state'],'uploaded');self.assertFalse(job['approved'])
        repeated=self.api.post('/api/media/uploads/'+str(asset.id)+'/complete').json()
        self.assertEqual(repeated['ingest']['job_id'],job['job_id'])
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*),min(input_sha256) FROM shadify_ingest_jobs WHERE asset_id=%s',(asset.id,)).fetchone(),
                             (1,hashlib.sha256(self.audio).hexdigest()))
        self.assertEqual(self.bytes.put(dto['upload']['url'],content=self.audio,headers=dto['upload']['headers']).status_code,409)

    def test_audio_and_cover_prescan_issue_and_old_style_get_head_range_deny(self):
        for cover in (False,True):
            asset,_,_=self.upload(cover=cover)
            path='/api/artists/'+str(self.artist)+'/tracks/'+str(self.track)
            reference='artwork_asset_id' if cover else 'original_asset_id'
            self.assertEqual(self.api.patch(path,json={reference:str(asset.id)}).status_code,200)
            self.assertEqual(self.api.get(path).json()['content'][reference],str(asset.id)) # Metadata survives refresh.
            self.assertEqual(self.api.post('/api/media/'+str(asset.id)+'/access').status_code,409)
            old=self.historical.private_access(admitted_actor('owner-a'),asset.id).url
            for method,headers in [('get',{}),('head',{}),('get',{'Range':'bytes=0-7'})]:
                self.assertEqual(getattr(self.bytes,method)(old,headers=headers).status_code,409)

    def test_historical_uploaded_row_and_issued_url_never_auto_approve(self):
        meta=self.metadata(self.legacy)
        self.assertTrue(meta['ingest']['legacy_unregistered']);self.assertFalse(meta['ingest']['approved'])
        self.assertEqual(self.api.post('/api/media/'+str(self.legacy.id)+'/access').status_code,409)
        self.assertEqual(self.bytes.get(self.old_url).status_code,409)
        self.assertEqual(self.bytes.head(self.old_url).status_code,409)
        self.assertEqual(self.bytes.get(self.old_url,headers={'Range':'bytes=-4'}).status_code,409)
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_jobs').fetchone()[0],0)

    def test_old_ready_delivery_does_not_publish_or_serve_or_leak_public_reference(self):
        ready=self.asset(self.audio,'audio_delivery','audio/wav','ready','free','published')
        with self.connect() as c:
            c.execute('UPDATE shadify_tracks SET delivery_asset_id=%s WHERE id=%s',(ready.id,self.track))
        path='/api/artists/'+str(self.artist)+'/tracks/'+str(self.track)
        self.assertEqual(self.api.put(path+'/publication',json={'state':'published'}).status_code,409)
        with self.connect() as c:
            c.execute("UPDATE shadify_tracks SET publish_state='published' WHERE id=%s",(self.track,))
            c.execute("UPDATE shadify_artists SET publish_state='published' WHERE id=%s",(self.artist,))
        old=self.historical.playback_access(self.track).url
        self.assertEqual(self.bytes.get(old).status_code,409)
        self.assertEqual(self.bytes.get('/media/v1/tracks/'+str(self.track)+'/access').status_code,409)
        public=self.api.get('/api/artists/'+str(self.artist)+'/public')
        self.assertEqual(public.status_code,200);self.assertNotIn(str(ready.id),public.text)

    def test_old_artwork_delivery_cannot_publish_profile_or_moment(self):
        cover=self.asset(PNG,'artwork_delivery','image/png','ready','free','published')
        with self.connect() as c:c.execute('UPDATE shadify_artists SET avatar_asset_id=%s WHERE id=%s',(cover.id,self.artist))
        self.assertEqual(self.api.put('/api/artists/'+str(self.artist)+'/publication',json={'state':'published'}).status_code,409)
        # Ordinary text metadata still works without a file reference.
        self.assertEqual(self.api.post('/api/artists/'+str(self.artist)+'/moments',json={'text':'Metadata only'}).status_code,201)

    def test_missing_ingest_schema_denies_old_token_without_transaction_poisoning(self):
        with self.connect() as c:c.execute('DROP TABLE shadify_ingest_approvals')
        self.assertEqual(self.api.post('/api/media/'+str(self.legacy.id)+'/access').status_code,503)
        self.assertEqual(self.bytes.get(self.old_url).status_code,503)
        # Profile/track metadata and ordinary identity are independent.
        self.assertEqual(self.api.get('/api/me').status_code,200)
        self.assertEqual(self.api.get('/api/artists/'+str(self.artist)+'/tracks/'+str(self.track)).status_code,200)

    def test_scanner_absent_unknown_is_retryable_and_not_malware_or_ready(self):
        asset,_,_=self.upload();job=self.job(asset)
        result=self.scanner().scan_job(job['job_id'],expected_revision=job['revision'])
        self.assertEqual(result['state'],'uploaded');self.assertTrue(result['retryable']);self.assertFalse(result['approved'])
        self.assertEqual(result['last_error'],'scanner_unavailable')
        again=self.scanner().scan_job(job['job_id'],expected_revision=result['revision'])
        self.assertEqual(again['state'],'uploaded')
        with self.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM shadify_ingest_scans WHERE verdict='unknown'").fetchone()[0],2)

    def test_scanner_timeout_error_and_bad_evidence_are_unknown_not_clean(self):
        for scanner,code in [(FixtureScanner(error=TimeoutError()),'scanner_timeout'),
                             (FixtureScanner(error=RuntimeError('sensitive-path-not-for-output')),'scanner_error'),
                             (FixtureScanner(bad_hash=True),'scanner_evidence_invalid')]:
            asset,_,_=self.upload();job=self.job(asset)
            result=self.scanner(scanner).scan_job(job['job_id'],expected_revision=1)
            self.assertEqual(result['state'],'uploaded');self.assertEqual(result['last_error'],code)
            self.assertNotIn('sensitive',str(self.metadata(asset)));self.assertFalse(self.job(asset)['approved'])

    def test_synthetic_clean_evidence_bound_to_job_hash_versions_time_is_not_ready(self):
        for cover in (False,True):
            asset,_,_=self.upload(cover=cover);job=self.job(asset)
            result=self.scanner(FixtureScanner()).scan_job(job['job_id'],expected_revision=1)
            self.assertEqual(result['state'],'validating');self.assertFalse(result['approved'])
            with self.connect() as c:
                record=c.execute('SELECT job_id,input_sha256,verdict,engine_version,definitions_version,scanned_at FROM shadify_ingest_scans WHERE job_id=%s',(UUID(job['job_id']),)).fetchone()
                self.assertEqual(record[:5],(UUID(job['job_id']),self.adapter.checksum(asset.key),'clean','SYNTHETIC-engine-1','SYNTHETIC-safe-definitions-1'))
                self.assertIsNotNone(record[5].tzinfo)
            self.assertEqual(self.api.post('/api/media/'+str(asset.id)+'/access').status_code,409)

    def test_safe_synthetic_infected_verdict_rejects_and_never_bypasses(self):
        asset,_,_=self.upload();job=self.job(asset)
        result=self.scanner(FixtureScanner('infected')).scan_job(job['job_id'],expected_revision=1)
        self.assertEqual(result['state'],'rejected');self.assertFalse(result['retryable']);self.assertEqual(result['last_error'],'malware_detected')
        with self.assertRaises(MediaError):self.scanner(FixtureScanner()).scan_job(job['job_id'],expected_revision=result['revision'])

    def test_changed_bytes_are_rejected_before_scanner_approval(self):
        asset,_,_=self.upload();job=self.job(asset)
        with self.adapter.parent(asset.key) as (parent,name):
            fd=os.open(name,os.O_WRONLY,dir_fd=parent);os.write(fd,b'changed');os.close(fd)
        result=self.scanner(FixtureScanner()).scan_job(job['job_id'],expected_revision=1)
        self.assertEqual(result['state'],'rejected');self.assertEqual(result['last_error'],'bytes_changed')

    def test_scan_loss_stale_retry_and_immutable_journal_do_not_repeat(self):
        asset,_,_=self.upload();job=self.job(asset)
        self.scanner(FixtureScanner()).scan_job(job['job_id'],expected_revision=1) # Reply deliberately discarded.
        self.assertEqual(self.job(asset)['state'],'validating')
        with self.assertRaises(MediaError):self.scanner(FixtureScanner()).scan_job(job['job_id'],expected_revision=1)
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_scans').fetchone()[0],1)
        with self.assertRaises(psycopg.errors.RaiseException):
            with self.connect() as c:c.execute('UPDATE shadify_ingest_scans SET verdict=verdict')

    def test_clean_scan_alone_cannot_set_ready_and_null_versions_do_not_pass_sql(self):
        asset,_,_=self.upload();job=self.job(asset)
        self.scanner(FixtureScanner()).scan_job(job['job_id'],expected_revision=1)
        with self.assertRaises(psycopg.errors.RaiseException):
            with self.connect() as c:
                c.execute("UPDATE shadify_ingest_jobs SET state='ready',revision=revision+1 WHERE id=%s",(UUID(job['job_id']),))
                c.execute('SET CONSTRAINTS ALL IMMEDIATE')
        self.assertEqual(self.job(asset)['state'],'validating')
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.connect() as c:c.execute("INSERT INTO shadify_ingest_scans(id,job_id,input_sha256,verdict) VALUES(%s,%s,%s,'clean')",(uuid4(),UUID(job['job_id']),self.adapter.checksum(asset.key)))

    def test_concurrent_scanner_claim_has_one_winner(self):
        asset,_,_=self.upload();job=self.job(asset);entered=Event();release=Event()
        class Blocking(FixtureScanner):
            def scan(self,*args,**kwargs):entered.set();assert release.wait(3);return super().scan(*args,**kwargs)
        with ThreadPoolExecutor(2) as pool:
            first=pool.submit(self.scanner(Blocking()).scan_job,job['job_id'],expected_revision=1)
            self.assertTrue(entered.wait(2))
            with self.assertRaises(MediaError):self.scanner(FixtureScanner()).scan_job(job['job_id'],expected_revision=1)
            release.set();self.assertEqual(first.result(3)['state'],'validating')

    def test_interrupted_scanner_leaves_durable_scanning_and_file_closed(self):
        asset,_,_=self.upload();job=self.job(asset);fds=[]
        class Interrupted:
            def scan(self,fd,**kwargs):fds.append(fd);raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.scanner(Interrupted()).scan_job(job['job_id'],expected_revision=1)
        self.assertEqual(self.job(asset)['state'],'scanning');self.assertFalse(self.job(asset)['approved'])
        with self.assertRaises(OSError):os.fstat(fds[0])
        with self.assertRaises(MediaError):self.scanner(FixtureScanner()).scan_job(job['job_id'],expected_revision=2)

    def test_other_user_cannot_read_job_upload_or_approve_by_request_fields(self):
        asset,_,_=self.upload();self.actor='owner-b'
        self.assertEqual(self.api.get('/api/media/'+str(asset.id)).status_code,404)
        self.assertEqual(self.api.post('/api/media/'+str(asset.id)+'/access').status_code,404)
        self.actor='owner-a'
        response=self.api.post('/api/media/uploads',json=dict(artist_id=str(self.artist),track_id=str(self.track),asset_type='audio_original',mime='audio/wav',size=len(self.audio),approved=True))
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.api.patch('/api/artists/'+str(self.artist)+'/tracks/'+str(self.track),json={'ingest_state':'ready'}).status_code,422)

    def pending(self,actor=None):
        api=self.api if actor is None else TestClient(create_app(self.service,lambda:actor,repository=self.repo,domain=DomainService(self.repo)))
        dto=api.post('/api/media/uploads',json=dict(artist_id=str(self.artist),track_id=str(self.track),asset_type='audio_original',mime='audio/wav',size=len(self.audio))).json()
        return api,dto

    def test_primary_admin_has_no_quarantine_scan_bypass(self):
        asset,_,_=self.upload(cover=True);self.actor='admin'
        self.assertEqual(self.api.get('/api/media/'+str(asset.id)).status_code,200)
        self.assertEqual(self.api.post('/api/media/'+str(asset.id)+'/access').status_code,409)
        self.assertFalse(self.metadata(asset)['ingest']['approved'])

    def test_expired_actor_or_url_cannot_upload_or_complete_quarantine(self):
        now=int(time.time());actor=admitted_actor('owner-a',issued=now,expires=now+60)
        api,dto=self.pending(actor)
        with patch('shadify_api.auth.time.time',return_value=now+61):
            self.assertEqual(self.bytes.put(dto['upload']['url'],content=self.audio,headers=dto['upload']['headers']).status_code,403)
            self.assertEqual(api.post('/api/media/uploads/'+dto['asset_id']+'/complete').status_code,401)
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_jobs').fetchone()[0],0)

    def test_identity_expiry_after_hash_rolls_back_upload_job_transition(self):
        now=int(time.time());actor=admitted_actor('owner-a',issued=now,expires=now+60);api,dto=self.pending(actor)
        self.assertEqual(self.bytes.put(dto['upload']['url'],content=self.audio,headers=dto['upload']['headers']).status_code,201)
        clock=[now];checksum=self.storage.checksum_asset
        def expires(asset):
            result=checksum(asset);clock[0]=actor.expires_at;return result
        with patch('shadify_api.auth.time.time',side_effect=lambda:clock[0]),patch.object(self.storage,'checksum_asset',side_effect=expires):
            self.assertEqual(api.post('/api/media/uploads/'+dto['asset_id']+'/complete').status_code,401)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT state FROM shadify_media_assets WHERE id=%s',(UUID(dto['asset_id']),)).fetchone()[0],'upload_pending')
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_jobs').fetchone()[0],0)

    def test_interrupted_or_invalid_put_never_becomes_ingest_ready(self):
        _,dto=self.pending();asset=self.repo.get(UUID(dto['asset_id']))
        self.assertEqual(self.bytes.put(dto['upload']['url'],content=self.audio,headers={**dto['upload']['headers'],'Content-Type':'image/png'}).status_code,422)
        def broken():yield self.audio[:8];raise RuntimeError('synthetic interrupted body')
        response=self.bytes.put(dto['upload']['url'],content=broken(),headers={**dto['upload']['headers'],'Content-Length':str(len(self.audio))})
        self.assertEqual(response.status_code,503)
        self.assertEqual(self.api.post('/api/media/uploads/'+dto['asset_id']+'/complete').status_code,503)
        self.assertEqual(self.metadata(asset)['state'],'upload_pending');self.assertIsNone(self.metadata(asset)['ingest'])
        self.assertFalse(list(Path(self.directory.name).rglob('.upload-*')))
        with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_jobs').fetchone()[0],0)
