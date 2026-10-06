"""Real private PG/HTTP and safe local WAV/PNG; synthetic tool/provider fixtures explicitly marked."""
import io,json,hashlib,os,struct,zlib,time,unittest,psycopg,subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from psycopg import sql
from pathlib import Path
from uuid import UUID,uuid4
from types import SimpleNamespace
from unittest.mock import patch
from authority_fixture import admitted_actor
from test_ingest_foundation import IngestFoundationTests,FixtureScanner
from shadify_api.ingest_pipeline import PipelineWorker
from shadify_api.ingest_worker import WorkerLimits
from shadify_api.musicbrainz import snapshot,ProviderUnavailable,url_for,query_metadata
from shadify_api.media_tools import container_check,inspect_mp3,bytes_fd,ToolFailure
from shadify_api.ingest_review import MatchReview
from shadify_api.media import MediaError

MBID='00000000-0000-4000-8000-000000000001'
def safe_png():
 def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
 return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0\xff\0\0'))+chunk(b'IEND',b'')

def synthetic_mp3(bitrate):
 index={320:14,128:9}[bitrate];header=0xfffb0000|(index<<12)
 return header.to_bytes(4,'big')+b'\0'*(144000*bitrate//44100-4)

class FixtureTools:
 # NOT a real encoder/decoder. Actual safe input container parsed; PG pipeline proof only.
 identity='SYNTHETIC media tool fixture'
 def __init__(self):self.encodes=[]
 def validate(self,fd,mime,*,deadline):
  container_check(fd,mime,104857600)
  audio=mime.startswith('audio/')
  quality={'codec':'SYNTHETIC','duration_ms':2,'channels':1,'sample_rate':8000} if audio else {'codec':'SYNTHETIC','width':1,'height':1,'frames':1}
  return dict(passed=True,fixture='SYNTHETIC not full decode',source_quality=quality),dict(complete=True,metadata={'title':'Quarantine proof','artist':'Test','isrc':'USAAA2600001','duration_ms':2} if audio else {})
 def process(self,fd,mime,validation,*,deadline,roles=None):
  roles=set(roles) if roles is not None else ({'high','low'} if mime.startswith('audio/') else {'cover'})
  self.encodes.extend(sorted(roles))
  return {role:('image/png',safe_png()) if role=='cover' else ('audio/mpeg',synthetic_mp3(320 if role=='high' else 128)) for role in roles}

class FixtureProvider:
 def __init__(self,*,ambiguous=False,failures=0):self.ambiguous=ambiguous;self.failures=failures;self.calls=0
 def lookup(self,metadata,*,deadline):
  self.calls+=1
  if self.failures:self.failures-=1;raise ProviderUnavailable()
  query=query_metadata(metadata)
  rows=[dict(id=MBID,title=query['title'],length=query['duration_ms'],**{'artist-credit':[{'name':query['artist']}]})]
  if self.ambiguous:rows=[]
  return {**snapshot(json.dumps(dict(isrc=query['isrc'],recordings=rows)).encode(),query),'fixture':'SYNTHETIC public metadata response'}

class PipelineTests(IngestFoundationTests):
 def setUp(self):
  super().setUp();self.apply('010_ingest_attempts.sql','011_ingest_processing.sql')
  self.tools=FixtureTools();self.provider=FixtureProvider();self.worker=PipelineWorker(connect=self.connect,adapters={'local':self.adapter},scanner=FixtureScanner(),tools=self.tools,provider=self.provider,limits=WorkerLimits(retry_seconds=1))
 def once(self):return self.worker.run_once()
 def audio_ready(self):
  asset,_,_=self.upload()
  states=[self.once()['state'] for _ in range(4)]
  self.assertEqual(states,['validating','matching','processing','ready']);return asset
 def outputs(self,asset):return self.metadata(asset)['outputs']
 def test_audio_and_cover_have_complete_atomic_approvals_and_immutable_masters(self):
  audio=self.audio_ready();self.assertEqual({v['role'] for v in self.outputs(audio)},{'master','high','low'})
  self.assertEqual(self.adapter.checksum(audio.key),hashlib.sha256(self.audio).hexdigest())
  with patch('test_ingest_foundation.PNG',safe_png()):cover,_,_=self.upload(cover=True)
  self.assertEqual([self.once()['state'] for _ in range(4)],['validating','matching','processing','ready'])
  self.assertEqual({v['role'] for v in self.outputs(cover)},{'master','cover'})
  with self.connect() as c:
   self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_approvals').fetchone()[0],5)
   self.assertEqual(c.execute("SELECT count(*) FROM shadify_ingest_match_decisions WHERE kind='cover' AND cover_audio_decision_id IS NOT NULL").fetchone()[0],1)
  self.assertEqual(self.api.post('/api/media/'+str(audio.id)+'/access').status_code,409)
  high=next(v for v in self.outputs(audio) if v['role']=='high')
  access=self.api.post('/api/media/'+high['asset_id']+'/access');self.assertEqual(access.status_code,200,access.text)
  self.assertEqual(self.bytes.get(access.json()['url']).status_code,200)
  count=len(self.tools.encodes);self.assertIsNone(self.once());self.assertEqual(len(self.tools.encodes),count)
 def test_provider_transient_failure_consumes_no_success_slot_then_retries(self):
  asset,_,_=self.upload();self.once();self.once();self.provider.failures=1
  result=self.once();self.assertEqual(result['state'],'matching');self.assertTrue(result['retryable'])
  with self.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM shadify_ingest_stages WHERE stage='matching'").fetchone()[0],0)
  self.worker.limits.retry_seconds=0
  self.assertEqual(self.once()['state'],'processing');self.assertEqual(self.once()['state'],'ready');self.assertEqual(self.provider.calls,2)
 def test_ambiguous_review_inserts_only_decision_then_worker_moves_state(self):
  asset,_,_=self.upload();self.provider.ambiguous=True
  self.once();self.once();self.assertEqual(self.once()['state'],'review_required')
  path='/api/media/'+str(asset.id)+'/match-review';view=self.api.get(path).json();revision=view['revision']
  body=dict(expected_revision=revision,selected_recording_mbid=None,reason='Original recording, completed lookup has no result',corrected_metadata={'title':'Real local fixture'})
  response=self.api.put(path,json=body);self.assertEqual(response.status_code,200,response.text)
  self.assertEqual(response.json()['state'],'review_required');self.assertEqual(response.json()['revision'],revision)
  self.assertEqual(self.api.put(path,json=body).status_code,409)
  self.assertEqual(self.once()['state'],'ready')
 def test_foreign_user_candidate_and_stale_review_deny(self):
  asset,_,_=self.upload();self.provider.ambiguous=True;self.once();self.once();self.once()
  path='/api/media/'+str(asset.id)+'/match-review';revision=self.api.get(path).json()['revision']
  body=dict(expected_revision=revision,selected_recording_mbid=str(uuid4()),reason='Foreign candidate')
  self.assertEqual(self.api.put(path,json=body).status_code,422)
  body['selected_recording_mbid']=None;body['expected_revision']=revision-1
  self.assertEqual(self.api.put(path,json=body).status_code,409)
  self.actor='owner-b';self.assertEqual(self.api.get(path).status_code,404);body['expected_revision']=revision
  self.assertEqual(self.api.put(path,json=body).status_code,404)
 def test_cover_waits_for_same_track_ready_audio_and_has_job_specific_evidence(self):
  with patch('test_ingest_foundation.PNG',safe_png()):cover,_,_=self.upload(cover=True)
  self.once();self.once();self.assertEqual(self.once()['last_error'],'provider_unavailable')
  with self.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM shadify_ingest_stages WHERE stage='matching'").fetchone()[0],0)
 def test_corrupt_container_rejects_without_matching_or_approval(self):
  asset,_,_=self.upload();self.once()
  with patch.object(self.tools,'validate',side_effect=ToolFailure('validation_invalid')):self.assertEqual(self.once()['state'],'rejected')
  self.assertEqual(self.outputs(asset),[])
 def test_expired_processing_attempt_cannot_commit_outputs_or_approval(self):
  asset,_,_=self.upload();self.once();self.once();self.once();self.worker.limits.lease_seconds=.02
  with self.worker.session() as connection:
   claim=self.worker.claim(connection);time.sleep(.03)
   with self.assertRaises(MediaError):self.worker.process(connection,claim)
  with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_approvals').fetchone()[0],0)
 def test_crash_after_installs_before_db_commit_reuses_verified_outputs_without_encode(self):
  asset,_,_=self.upload();self.once();self.once();self.once()
  original=self.worker._insert_stage
  def crash(c,job,scan,stage,payload):
   if stage=='processing':raise RuntimeError('SYNTHETIC crash before DB commit')
   return original(c,job,scan,stage,payload)
  self.worker.limits.lease_seconds=.2
  with patch.object(self.worker,'_insert_stage',side_effect=crash):
   with self.assertRaises(RuntimeError):self.once()
  with self.connect() as c:
   self.assertEqual(c.execute("SELECT count(*) FROM shadify_ingest_stages WHERE stage='prepared_outputs'").fetchone()[0],1)
   self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_approvals').fetchone()[0],0)
   self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_variants').fetchone()[0],0)
  count=len(self.tools.encodes);time.sleep(.21);self.worker.limits.lease_seconds=360
  self.assertEqual(self.once()['state'],'ready');self.assertEqual(len(self.tools.encodes),count)
  self.assertIsNone(self.once())
 def test_crash_after_manifest_before_any_install_regenerates_missing_outputs_only(self):
  asset,_,_=self.upload();self.once();self.once();self.once();self.worker.limits.lease_seconds=.2
  with patch.object(self.adapter,'install',side_effect=RuntimeError('SYNTHETIC crash before install')):
   with self.assertRaises(RuntimeError):self.once()
  time.sleep(.21);self.worker.limits.lease_seconds=360
  self.assertEqual(self.once()['state'],'ready')
  self.assertEqual(self.tools.encodes,['high','low','high','low'])
 def test_same_table_exclusion_fences_parallel_pipeline_and_scanner(self):
  asset,_,_=self.upload()
  with self.worker.session() as connection:
   self.worker.claim(connection);self.assertIsNone(self.worker.run_once())
 def test_review_expiry_after_insert_rolls_back_decision(self):
  asset,_,_=self.upload();self.provider.ambiguous=True;self.once();self.once();self.once()
  revision=self.job(asset)['revision'];calls=[0]
  from shadify_api.auth import require_fresh_actor
  def expire(actor):
   calls[0]+=1
   if calls[0]>=1:raise MediaError(401,'SYNTHETIC expiry after wait')
   return require_fresh_actor(actor)
  with patch('shadify_api.ingest_review.require_fresh_actor',side_effect=expire):
   with self.assertRaises(MediaError):self.repo.match_review(admitted_actor('owner-a'),asset.id,MatchReview(expected_revision=revision,reason='Explicit reviewed no-match'))
  with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_match_decisions').fetchone()[0],0)
 def test_delegate_review_waiting_for_revocation_cannot_commit(self):
  asset,_,_=self.upload();self.provider.ambiguous=True;self.once();self.once();self.once()
  self.repo.set_delegate(admitted_actor('admin'),'listener',['manage_content'],expected_revision=self.admin_revision())
  body=MatchReview(expected_revision=self.job(asset)['revision'],reason='Review must keep permission')
  entered=Event()
  with self.connect() as c:
   with c.cursor() as cursor:cursor.execute('SELECT singleton FROM shadify_app_admin FOR UPDATE')
   with ThreadPoolExecutor(max_workers=1) as pool:
    def review():
     entered.set()
     try:self.repo.match_review(admitted_actor('listener'),asset.id,body)
     except MediaError as exc:return exc.status
    future=pool.submit(review);self.assertTrue(entered.wait(2));time.sleep(.03)
    # Same locked transaction commits revocation; waiting actor must re-read.
    c.execute("DELETE FROM shadify_app_admin_delegates WHERE user_id=(SELECT id FROM shadify_users WHERE auth_account_id='listener')")
    c.commit();self.assertEqual(future.result(3),404)
  with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_match_decisions').fetchone()[0],0)
 def test_api_role_can_insert_review_but_cannot_change_job_state(self):
  asset,_,_=self.upload();self.provider.ambiguous=True;self.once();self.once();self.once()
  role='pipeline_api_'+uuid4().hex
  with self.connect() as c:
   c.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
   c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(self.schema),sql.Identifier(role)))
   c.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(self.schema),sql.Identifier(role)))
   # All actual row locks have column UPDATE permission; no state/revision writes.
   for table,column in [('shadify_artists','id'),('shadify_tracks','id'),('shadify_ingest_jobs','id'),('shadify_users','id'),('shadify_app_admin','singleton')]:
    c.execute(sql.SQL('GRANT UPDATE ({}) ON {} TO {}').format(sql.Identifier(column),sql.Identifier(table),sql.Identifier(role)))
   c.execute(sql.SQL('GRANT INSERT ON shadify_ingest_match_decisions TO {}').format(sql.Identifier(role)))
  original=self.repo.connect
  def restricted():
   connection=original();connection.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)));return connection
  try:
   self.repo.connect=restricted
   result=self.repo.match_review(admitted_actor('owner-a'),asset.id,MatchReview(expected_revision=self.job(asset)['revision'],reason='Least privilege owner review'))
   self.assertEqual(result['state'],'review_required')
   with restricted() as c:
    with self.assertRaises(psycopg.errors.InsufficientPrivilege):c.execute("UPDATE shadify_ingest_jobs SET state='ready',revision=revision+1")
  finally:
   self.repo.connect=original
   with self.connect() as c:c.execute(sql.SQL('DROP OWNED BY {};DROP ROLE {}').format(sql.Identifier(role),sql.Identifier(role)))


 def test_worker_role_requires_no_artist_admin_write_or_identity_access(self):
  asset,_,_=self.upload();role='pipeline_worker_'+uuid4().hex
  with self.connect() as c:
   c.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
   c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(self.schema),sql.Identifier(role)))
   for table in ('shadify_media_assets','shadify_ingest_jobs','shadify_ingest_attempts','shadify_ingest_scans','shadify_ingest_stages','shadify_ingest_match_decisions','shadify_ingest_variants','shadify_ingest_approvals'):
    c.execute(sql.SQL('GRANT SELECT ON {} TO {}').format(sql.Identifier(table),sql.Identifier(role)))
   for table in ('shadify_media_assets','shadify_ingest_attempts','shadify_ingest_scans','shadify_ingest_stages','shadify_ingest_match_decisions','shadify_ingest_variants','shadify_ingest_approvals'):
    c.execute(sql.SQL('GRANT INSERT ON {} TO {}').format(sql.Identifier(table),sql.Identifier(role)))
   c.execute(sql.SQL('GRANT SELECT(id,name) ON shadify_artists TO {}').format(sql.Identifier(role)))
   c.execute(sql.SQL('GRANT SELECT(id,artist_id,title) ON shadify_tracks TO {}').format(sql.Identifier(role)))
   c.execute(sql.SQL('GRANT UPDATE(state,revision,last_error,updated_at) ON shadify_ingest_jobs TO {}').format(sql.Identifier(role)))
   c.execute(sql.SQL('GRANT UPDATE(finished_at,outcome) ON shadify_ingest_attempts TO {}').format(sql.Identifier(role)))
  def restricted():
   connection=self.connect();connection.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)));return connection
  original=self.worker.connect
  try:
   self.worker.connect=restricted
   self.assertEqual([self.once()['state'] for _ in range(4)],['validating','matching','processing','ready'])
   with restricted() as c:
    with self.assertRaises(psycopg.errors.InsufficientPrivilege):c.execute("UPDATE shadify_artists SET name='No worker mutation'")
   with restricted() as c:
    with self.assertRaises(psycopg.errors.InsufficientPrivilege):c.execute('SELECT * FROM shadify_app_admin')
  finally:
   self.worker.connect=original
   with self.connect() as c:c.execute(sql.SQL('DROP OWNED BY {};DROP ROLE {}').format(sql.Identifier(role),sql.Identifier(role)))
 def test_corrupt_installed_candidate_after_crash_never_overwrites_or_approves(self):
  asset,_,_=self.upload();self.once();self.once();self.once();original=self.worker._insert_stage;self.worker.limits.lease_seconds=.2
  def crash(c,job,scan,stage,payload):
   if stage=='processing':raise RuntimeError('SYNTHETIC interrupted commit')
   return original(c,job,scan,stage,payload)
  with patch.object(self.worker,'_insert_stage',side_effect=crash):
   with self.assertRaises(RuntimeError):self.once()
  with self.connect() as c:
   key=c.execute("SELECT payload->'outputs'->'high'->>'key' FROM shadify_ingest_stages WHERE stage='prepared_outputs'").fetchone()[0]
  path=Path(self.directory.name)/key;data=path.read_bytes();path.write_bytes(data[:-1]+b'X')
  time.sleep(.21);self.worker.limits.lease_seconds=360
  self.assertEqual(self.once()['state'],'rejected');self.assertEqual(path.read_bytes(),data[:-1]+b'X')
  with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_approvals').fetchone()[0],0)


class HistoricalApprovalTests(IngestFoundationTests):
 def test_old_structural_approval_is_inactive_under011_without_pipeline_evidence(self):
  output,job=self.approved_fixture()
  self.apply('010_ingest_attempts.sql','011_ingest_processing.sql')
  self.assertEqual(self.api.post('/api/media/'+str(output.id)+'/access').status_code,409)

class MediaSandboxTests(unittest.TestCase):
 def test_every_media_preflight_decode_encode_uses_real_bwrap_no_credentials_or_network(self):
  from test_clamav_worker import fixture_runtime
  from shadify_api.media_tools import MediaTools
  import tempfile
  runtime=fixture_runtime()
  # Real native namespace, synthetic protocol only; production rejects Python closure.
  reporter="""#!/usr/bin/python3
import sys,os,json
from pathlib import Path
name=Path(sys.argv[0]).name
if '-version' in sys.argv:
 print(name+' version SYNTHETIC copyright test fixture');sys.exit(0)
data=sys.stdin.buffer.read()
if not Path('/proc/self/fd/0').is_fifo():raise RuntimeError('input fd leaked')
if Path('/etc/worker-db.json').exists() or 'SYNTHETIC_DB_PASSWORD' in os.environ:raise RuntimeError('credential leak')
if name=='ffprobe':
 print(json.dumps({'streams':[{'codec_type':'audio','codec_name':'pcm_s16le','sample_rate':'8000','channels':1,'bits_per_sample':16}], 'format':{'format_name':'wav','tags':{'title':'Safe fixture','artist':'Test'}}}))
else:sys.stdout.buffer.write(data[44:])
"""
  for tool in ('ffmpeg','ffprobe'):
   path=runtime/'usr/bin'/tool;path.write_text(reporter);path.chmod(0o755)
  import wave
  audio=io.BytesIO()
  with wave.open(audio,'wb') as output:output.setnchannels(1);output.setsampwidth(2);output.setframerate(8000);output.writeframes(b'\0'*32)
  with tempfile.TemporaryDirectory(prefix='shadify-media-sandbox-proof-') as scratch:
   tools=MediaTools(bwrap='/usr/bin/bwrap',runtime_root=runtime,prlimit='/usr/bin/prlimit',temp_dir=scratch,version='SYNTHETIC',limits=WorkerLimits())
   with bytes_fd(audio.getvalue()) as fd,patch.dict(os.environ,SYNTHETIC_DB_PASSWORD='SYNTHETIC-NOT-LIVE'):
    validation,extraction=tools.validate(fd,'audio/wav',deadline=time.monotonic()+5)
   self.assertTrue(validation['passed']);self.assertEqual(extraction['metadata']['duration_ms'],2)
   tools.sandbox.bwrap='/usr/bin/false'
   with self.assertRaises(ToolFailure) as error:tools.preflight(deadline=time.monotonic()+5)
   self.assertEqual(error.exception.code,'processing_unavailable')

class MetadataAndContainerTests(unittest.TestCase):
 def test_automatic_requires_exact_unique_isrc_names_duration_and_complete_response(self):
  query=query_metadata(dict(title='A song',artist='An artist',isrc='USAAA2600001',duration_ms=10000))
  row=dict(id=MBID,title='A song',length=10000,**{'artist-credit':[{'name':'An artist'}]})
  self.assertEqual(snapshot(json.dumps(dict(isrc=query['isrc'],recordings=[row])).encode(),query)['automatic_mbid'],MBID)
  row['length']=20000;self.assertIsNone(snapshot(json.dumps(dict(isrc=query['isrc'],recordings=[row])).encode(),query)['automatic_mbid'])
  with self.assertRaises(ProviderUnavailable):snapshot(b'{"error":"retry"}',query)
  with self.assertRaises(ProviderUnavailable):snapshot(b'X'*1048577,query)
 def test_lookup_sends_only_bounded_musical_fields_to_fixed_public_host(self):
  query=query_metadata(dict(title='A " title',artist='Artist',duration_ms=10000,account_id='SYNTHETIC-SECRET',key='private/path'))
  url=url_for(query);self.assertTrue(url.startswith('https://musicbrainz.org/ws/2/recording?'));self.assertNotIn('SECRET',url);self.assertNotIn('private',url)
 def test_real_generated_png_container_and_truncation_crc_trailing_data(self):
  good=safe_png()
  with bytes_fd(good) as fd:container_check(fd,'image/png',1000)
  for bad in (good[:-1],good+b'payload',good[:20]+b'X'+good[21:]):
   with bytes_fd(bad) as fd:
    with self.assertRaises(ToolFailure):container_check(fd,'image/png',1000)
 def test_mpeg1_bitrate_frame_and_truncation_validation(self):
  # Header-only synthetic frame mechanics, explicitly NOT decoded audio.
  inspect_mp3(synthetic_mp3(320),320);inspect_mp3(synthetic_mp3(128),128)
  with self.assertRaises(ToolFailure):inspect_mp3(synthetic_mp3(128),320)
  with self.assertRaises(ToolFailure):inspect_mp3(synthetic_mp3(320)[:-1],320)

def load_tests(loader,tests,pattern):
 suite=loader.loadTestsFromTestCase(MetadataAndContainerTests)
 suite.addTests(loader.loadTestsFromTestCase(MediaSandboxTests))
 suite.addTest(HistoricalApprovalTests('test_old_structural_approval_is_inactive_under011_without_pipeline_evidence'))
 suite.addTests(PipelineTests(name) for name in PipelineTests.__dict__ if name.startswith('test_'))
 return suite
