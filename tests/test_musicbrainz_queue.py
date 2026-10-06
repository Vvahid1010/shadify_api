"""Disposable PostgreSQL + mocked public transport; no real traffic or grants."""
import hashlib,io,json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from threading import Event
from unittest.mock import patch
from uuid import uuid4
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from isolated_postgres import IsolatedPostgres
from shadify_api.musicbrainz import ProviderUnavailable,fetch_response,query_metadata,snapshot
from shadify_api.musicbrainz_queue import QueuedMusicBrainzProvider,canonical_request,retry_seconds

QUERY=dict(title='Synthetic song',artist='Fixture Artist',isrc='USAAA2600001',duration_ms=1000)
AGENT='Shadify/test (https://shadify.org)'
MBID='00000000-0000-4000-8000-000000000001'
def response(*,count=1,status=200):
 document={'isrc':QUERY['isrc'],'recordings':[{'id':MBID,'title':QUERY['title'],'length':1000,'artist-credit':[{'name':QUERY['artist']}]}] if count else [],'count':count}
 return {'status':status,'document':document,'response_sha256':hashlib.sha256(json.dumps(document).encode()).hexdigest()}

class QueueTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.cluster=IsolatedPostgres();cls.server=cls.cluster.__enter__()
 @classmethod
 def tearDownClass(cls):cls.cluster.__exit__(None,None,None)
 def setUp(self):
  self.schema='mb_'+uuid4().hex
  with psycopg.connect(**self.server.connection,autocommit=True) as c:c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
  self.connect=lambda:psycopg.connect(**self.server.connection,options='-c search_path='+self.schema)
  with self.connect() as c:c.execute((Path(__file__).resolve().parents[1]/'migrations/012_musicbrainz_coordination.sql').read_text())
  self.live=QueuedMusicBrainzProvider(connect=self.connect,user_agent=AGENT,mode='b_live')
  self.offline=QueuedMusicBrainzProvider(connect=self.connect,user_agent=AGENT,mode='cache_only')
 def tearDown(self):
  with psycopg.connect(**self.server.connection,autocommit=True) as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
 def enqueue(self,query=QUERY,provider=None):
  with self.assertRaises(ProviderUnavailable):(provider or self.live).lookup(query,deadline=time.monotonic()+5)
 def scalar(self,statement):
  with self.connect() as c:return c.execute(statement).fetchone()[0]
 def ready(self):
  with self.connect() as c:
   c.execute("UPDATE shadify_mb_rate SET next_at=clock_timestamp()-interval '1 second',cooldown_until=clock_timestamp()-interval '1 second'")
   c.execute("UPDATE shadify_mb_queue SET next_at=clock_timestamp()-interval '1 second'")
 def test_concurrent_misses_enqueue_once_and_concurrent_dispatch_fetch_once(self):
  with ThreadPoolExecutor(6) as pool:list(pool.map(lambda _:self.enqueue(),range(6)))
  self.assertEqual(self.scalar('SELECT count(*) FROM shadify_mb_queue'),1)
  entered=Event();release=Event()
  def fetch(request,**kwargs):entered.set();self.assertTrue(release.wait(3));return response()
  with patch.object(self.live,'_transport',side_effect=fetch) as fetch_mock,ThreadPoolExecutor(2) as pool:
   first=pool.submit(self.live.dispatch_once);self.assertTrue(entered.wait(3))
   self.assertIsNone(pool.submit(self.live.dispatch_once).result(3));release.set();self.assertEqual(first.result(3),'cached')
  fetch_mock.assert_called_once()
 def test_partial_nonempty_response_retries_without_cache_or_automatic_match(self):
  for total in (2,5):
   with self.subTest(total=total):
    self.enqueue();self.ready()
    with patch.object(self.live,'_transport',return_value=response(count=total)):
     self.assertEqual(self.live.dispatch_once(),'retry')
    self.assertEqual(self.scalar('SELECT count(*) FROM shadify_mb_cache'),0)
    self.assertEqual(self.scalar('SELECT state FROM shadify_mb_queue'),'queued')
    self.assertEqual(self.scalar('SELECT last_error FROM shadify_mb_queue'),'provider_unavailable')
    with self.assertRaises(ProviderUnavailable):self.offline.lookup(QUERY,deadline=time.monotonic()+5)

 def test_old_recorded_partial_cache_is_rejected_per_file_without_traffic(self):
  key,request,_=canonical_request(QUERY)
  for total in (2,5):
   with self.subTest(total=total),self.connect() as c:
    c.execute('DELETE FROM shadify_mb_cache')
    c.execute("INSERT INTO shadify_mb_cache(request_key,request,response,fetched_at,expires_at,response_status) VALUES(%s,%s,%s,statement_timestamp(),statement_timestamp()+interval '24 hours',200)",(key,Jsonb(request),Jsonb(response(count=total))))
   with patch.object(self.offline,'_transport',side_effect=AssertionError('No cache fallback')) as transport:
    with self.assertRaises(ProviderUnavailable):self.offline.lookup(QUERY,deadline=time.monotonic()+5)
    transport.assert_not_called()

 def test_cache_reuses_response_recomputes_each_file_confidence_and_never_network(self):
  self.enqueue()
  with patch.object(self.live,'_transport',return_value=response()):self.assertEqual(self.live.dispatch_once(),'cached')
  with patch.object(self.live,'_transport',side_effect=AssertionError('cache hit no dispatch')) as transport:
   good=self.live.lookup(QUERY,deadline=time.monotonic()+5)
   ambiguous=self.live.lookup({**QUERY,'duration_ms':15000},deadline=time.monotonic()+5)
   self.assertEqual(good['automatic_mbid'],MBID);self.assertIsNone(ambiguous['automatic_mbid']);transport.assert_not_called()
  self.assertNotIn('automatic_mbid',str(self.scalar('SELECT response FROM shadify_mb_cache')))
 def test_wsl_miss_never_enqueues_or_dispatches_and_recorded_cache_works(self):
  self.enqueue(provider=self.offline)
  with patch.object(self.offline,'_transport',side_effect=AssertionError('WSL no live fallback')) as transport:
   self.assertIsNone(self.offline.dispatch_once());transport.assert_not_called()
  self.assertEqual(self.scalar('SELECT count(*) FROM shadify_mb_queue'),0)
  self.enqueue()
  with patch.object(self.live,'_transport',return_value=response()):self.live.dispatch_once()
  self.assertTrue(self.offline.lookup(QUERY,deadline=time.monotonic()+5)['cache_hit'])
 def test_completion_spacing_not_reservation_spacing(self):
  self.enqueue();second={**QUERY,'isrc':'USAAA2600002'};self.enqueue(second)
  completed=[]
  def fetch(request,**kwargs):time.sleep(.15);completed.append(time.monotonic());return response()
  with patch.object(self.live,'_transport',side_effect=fetch) as transport:
   self.live.dispatch_once();self.assertIsNone(self.live.dispatch_once());time.sleep(1.12)
   self.live.dispatch_once();self.assertEqual(transport.call_count,2)
  self.assertGreaterEqual(completed[1]-completed[0],1.1)
 def test_429_shared_cooldown_survives_new_provider_and_blocks_other_request(self):
  self.enqueue();self.enqueue({**QUERY,'isrc':'USAAA2600002'})
  def late_response(request,**kwargs):time.sleep(.1);return {'status':429,'retry_after':'60'}
  with patch.object(self.live,'_transport',side_effect=late_response):self.assertEqual(self.live.dispatch_once(),'retry')
  other=QueuedMusicBrainzProvider(connect=self.connect,user_agent=AGENT,mode='b_live')
  with patch.object(other,'_transport') as transport:self.assertIsNone(other.dispatch_once());transport.assert_not_called()
  self.assertGreater(self.scalar('SELECT extract(epoch FROM cooldown_until-clock_timestamp()) FROM shadify_mb_rate'),58)
  self.assertEqual(self.scalar('SELECT count(*) FROM shadify_mb_cache'),0)
 def test_late_503_preserves_http_date_and_long_delay_pauses_instead_of_clipping(self):
  self.enqueue()
  date=(datetime.now(timezone.utc)+timedelta(days=2)).strftime('%a, %d %b %Y %H:%M:%S GMT')
  with patch.object(self.live,'_transport',return_value={'status':503,'retry_after':date}):self.live.dispatch_once()
  self.assertTrue(self.scalar('SELECT paused FROM shadify_mb_rate'))
  self.assertGreater(self.scalar('SELECT extract(epoch FROM cooldown_until-clock_timestamp()) FROM shadify_mb_rate'),170000)
  self.assertEqual(self.scalar('SELECT state FROM shadify_mb_queue'),'paused')
 def test_unknown_inflight_on_restart_cannot_overlap_or_become_no_match(self):
  key=canonical_request(QUERY)[0];self.enqueue()
  with self.connect() as c:
   c.execute("UPDATE shadify_mb_queue SET state='inflight',attempts=1")
   c.execute("UPDATE shadify_mb_rate SET inflight_until=clock_timestamp()+interval '14 seconds',request_key=%s,attempt_id=%s",(key,uuid4()))
  with patch.object(self.live,'_transport') as transport:self.assertIsNone(self.live.dispatch_once());transport.assert_not_called()
  with self.connect() as c:c.execute("UPDATE shadify_mb_rate SET inflight_until=clock_timestamp()-interval '1 second'")
  with patch.object(self.live,'_transport') as transport:self.assertIsNone(self.live.dispatch_once());transport.assert_not_called()
  self.assertEqual(self.scalar('SELECT state FROM shadify_mb_queue'),'queued');self.assertEqual(self.scalar('SELECT count(*) FROM shadify_mb_cache'),0)
 def test_timeout_error_and_partial_never_negative_cache(self):
  for value in (TimeoutError(),{'status':200,'document':{'recordings':[],'count':10},'response_sha256':'a'*64},{'status':None}):
   with self.subTest(value=value):
    self.enqueue();self.ready()
    options={'side_effect':value} if isinstance(value,Exception) else {'return_value':value}
    with patch.object(self.live,'_transport',**options):self.assertEqual(self.live.dispatch_once(),'retry')
    self.assertEqual(self.scalar('SELECT count(*) FROM shadify_mb_cache'),0)
 def test_explicit_404_negative_ttl_and_positive_ttl_are_bounded(self):
  self.enqueue()
  with patch.object(self.live,'_transport',return_value=response(count=0,status=404)):self.live.dispatch_once()
  self.assertLessEqual(self.scalar('SELECT extract(epoch FROM expires_at-fetched_at) FROM shadify_mb_cache'),3600)
  result=self.offline.lookup(QUERY,deadline=time.monotonic()+5);self.assertEqual(result['candidates'],[]);self.assertIsNone(result['automatic_mbid'])
 def test_expired_cache_requeues_completed_key_and_retention_prunes(self):
  self.enqueue()
  with patch.object(self.live,'_transport',return_value=response()):self.live.dispatch_once()
  with self.connect() as c:c.execute("UPDATE shadify_mb_cache SET fetched_at=clock_timestamp()-interval '2 days',expires_at=clock_timestamp()-interval '25 hours'")
  self.enqueue();self.assertEqual(self.scalar('SELECT state FROM shadify_mb_queue'),'queued')
  with self.connect() as c:c.execute("UPDATE shadify_mb_queue SET state='done',updated_at=clock_timestamp()-interval '8 days'")
  self.enqueue();self.assertEqual(self.scalar('SELECT count(*) FROM shadify_mb_queue'),1)
 def test_capacity_and_retry_attempt_budget(self):
  with patch('shadify_api.musicbrainz_queue.QUEUE_CAPACITY',1):
   self.enqueue();self.enqueue({**QUERY,'isrc':'USAAA2600002'});self.assertEqual(self.scalar('SELECT count(*) FROM shadify_mb_queue'),1)
  for _ in range(5):
   self.ready()
   with patch.object(self.live,'_transport',return_value={'status':None}):self.live.dispatch_once()
  self.assertEqual(self.scalar('SELECT state FROM shadify_mb_queue'),'paused');self.assertEqual(self.scalar('SELECT attempts FROM shadify_mb_queue'),5)
 def test_public_request_key_ignores_duration_and_job_fields(self):
  a=canonical_request(QUERY);b=canonical_request({**QUERY,'duration_ms':5000,'account_id':'PRIVATE','job':'PRIVATE'})
  self.assertEqual(a[0],b[0]);self.assertNotIn('PRIVATE',str(a[1]));self.assertNotIn('automatic_mbid',str(a[1]))
 def test_retry_after_invalid_and_dates(self):
  now=datetime.now(timezone.utc)
  self.assertEqual(retry_seconds('60',now),60)
  for value in ('-1','nan','x'*257,None):self.assertIsNone(retry_seconds(value,now))
 def test_network_occurs_after_durable_inflight_and_outside_transaction(self):
  self.enqueue()
  def fetch(request,**kwargs):
   self.assertEqual(self.scalar('SELECT state FROM shadify_mb_queue'),'inflight')
   self.assertIsNotNone(self.scalar('SELECT inflight_until FROM shadify_mb_rate'))
   self.assertEqual(self.scalar("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='idle in transaction'"),0)
   return response()
  with patch.object(self.live,'_transport',side_effect=fetch):self.assertEqual(self.live.dispatch_once(),'cached')

 def test_cache_capacity_evicts_oldest_and_positive_ttl_is_24_hours(self):
  self.enqueue()
  with patch.object(self.live,'_transport',return_value=response()):self.live.dispatch_once()
  self.assertEqual(self.scalar('SELECT extract(epoch FROM expires_at-fetched_at) FROM shadify_mb_cache'),86400)
  second={**QUERY,'isrc':'USAAA2600002'};self.enqueue(second);self.ready()
  result=response();result['document']['isrc']=second['isrc']
  with patch('shadify_api.musicbrainz_queue.CACHE_CAPACITY',1),patch.object(self.live,'_transport',return_value=result):self.live.dispatch_once()
  self.assertEqual(self.scalar('SELECT count(*) FROM shadify_mb_cache'),1)
  self.assertEqual(self.scalar('SELECT request_key FROM shadify_mb_cache'),canonical_request(second)[0])
 def test_elapsed_retry_budget_pauses_without_fetch(self):
  self.enqueue()
  with self.connect() as c:c.execute("UPDATE shadify_mb_queue SET created_at=clock_timestamp()-interval '25 hours'")
  with patch.object(self.live,'_transport') as transport:self.assertIsNone(self.live.dispatch_once());transport.assert_not_called()
  self.assertEqual(self.scalar('SELECT last_error FROM shadify_mb_queue'),'retry_budget_exhausted')

 def test_long_numeric_and_unrepresentable_cooldowns_never_retry_faster(self):
  for value in ('10000000000','9'*128,'numeric-overflow'):
   with self.subTest(value=value):
    with self.connect() as c:
     c.execute('DELETE FROM shadify_mb_queue');c.execute('UPDATE shadify_mb_rate SET paused=false,cooldown_until=clock_timestamp(),next_at=clock_timestamp()')
    self.enqueue()
    with patch.object(self.live,'_transport',return_value={'status':429,'retry_after':value}):self.live.dispatch_once()
    self.assertTrue(self.scalar('SELECT paused FROM shadify_mb_rate'))
    with patch.object(self.live,'_transport') as transport:self.assertIsNone(self.live.dispatch_once());transport.assert_not_called()
  self.assertEqual(retry_seconds('10000000000',datetime.now(timezone.utc)),10000000000)

 def test_proposed_worker_role_can_coordinate_without_authority_access(self):
  role='mb_worker_'+uuid4().hex
  with self.connect() as c:
   c.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
   c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(self.schema),sql.Identifier(role)))
   c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON shadify_mb_cache,shadify_mb_queue TO {}').format(sql.Identifier(role)))
   c.execute(sql.SQL('GRANT SELECT,UPDATE ON shadify_mb_rate TO {}').format(sql.Identifier(role)))
   c.execute('CREATE TABLE private_authority(id integer)')
  def connect():
   c=self.connect();c.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)));return c
  provider=QueuedMusicBrainzProvider(connect=connect,user_agent=AGENT,mode='b_live')
  try:
   self.enqueue(provider=provider)
   with patch.object(provider,'_transport',return_value=response()):self.assertEqual(provider.dispatch_once(),'cached')
   self.assertTrue(provider.lookup(QUERY,deadline=time.monotonic()+5)['cache_hit'])
   for query in ('SELECT * FROM private_authority','DELETE FROM shadify_mb_rate','INSERT INTO shadify_mb_rate(singleton) VALUES(false)'):
    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
     with connect() as c:c.execute(query)
  finally:
   with self.connect() as c:c.execute(sql.SQL('DROP OWNED BY {}; DROP ROLE {}').format(sql.Identifier(role),sql.Identifier(role)))

class TransportTests(unittest.TestCase):
 def test_snapshot_rejects_partial_nonempty_and_malformed_counts_and_pages(self):
  document=response()['document']
  for fields in ({'count':2},{'count':5},{'count':0},{'count':-1},{'count':True},{'count':'1'},
                 {'count':None},{'count':1.0},{'count':1,'recording-count':2},
                 {'offset':1},{'offset':-1},{'offset':True},{'offset':'0'},
                 {'recording-offset':1}):
   with self.subTest(fields=fields),self.assertRaises(ProviderUnavailable):
    snapshot(json.dumps({**document,**fields}).encode(),query_metadata(QUERY))
  search=query_metadata({**QUERY,'isrc':''})
  with self.assertRaises(ProviderUnavailable):snapshot(json.dumps({'recordings':document['recordings']}).encode(),search)
  self.assertIsNone(snapshot(json.dumps(document).encode(),search)['automatic_mbid'])
  self.assertEqual(snapshot(json.dumps(document).encode(),query_metadata(QUERY))['automatic_mbid'],MBID)
  with self.assertRaises(ProviderUnavailable):snapshot(json.dumps({**document,'count':6,'recordings':document['recordings']*6}).encode(),query_metadata(QUERY))

 def test_transport_partial_response_never_normalizes_into_cacheable_evidence(self):
  from email.message import Message
  for total in (2,5):
   headers=Message();headers['Content-Type']='application/json'
   with self.subTest(total=total),patch('shadify_api.musicbrainz.urllib.request.build_opener') as builder:
    remote=builder.return_value.open.return_value.__enter__.return_value
    remote.status=200;remote.headers=headers
    remote.read.return_value=json.dumps(response(count=total)['document']).encode()
    with self.assertRaises(ProviderUnavailable):fetch_response(query_metadata(QUERY),AGENT)

 def test_expired_dispatch_child_deadline_prevents_network(self):
  code="from shadify_api.musicbrainz import bind_request_deadline; import time; bind_request_deadline(time.monotonic()-1); raise AssertionError('expired reservation admitted')"
  result=subprocess.run([sys.executable,'-c',code],capture_output=True)
  self.assertNotEqual(result.returncode,0);self.assertIn(b'Provider reservation expired',result.stderr)
 def test_kernel_deadline_kills_child_without_parent_enforcement(self):
  code="from shadify_api.musicbrainz import bind_request_deadline; import time; bind_request_deadline(time.monotonic()+.1); time.sleep(30)"
  started=time.monotonic();result=subprocess.run([sys.executable,'-c',code],capture_output=True,timeout=3)
  self.assertEqual(result.returncode,-14);self.assertLess(time.monotonic()-started,3)

 def test_http_429_status_retry_after_retained_without_body_or_secret(self):
  import urllib.error
  from email.message import Message
  headers=Message();headers['Retry-After']='90'
  error=urllib.error.HTTPError('https://musicbrainz.org/ws/2/isrc/'+QUERY['isrc'],429,'',headers,io.BytesIO(b'PRIVATE REMOTE DIAGNOSTIC'))
  with patch('shadify_api.musicbrainz.urllib.request.build_opener') as builder:
   builder.return_value.open.side_effect=error
   self.assertEqual(fetch_response(query_metadata(QUERY),AGENT),{'status':429,'retry_after':'90'})
 def test_delayed_child_after_parent_death_cannot_reach_network(self):
  code="from shadify_api.musicbrainz import bind_parent; import os; bind_parent(os.getppid()+100000); raise AssertionError('custody accepted')"
  result=subprocess.run([sys.executable,'-c',code],capture_output=True)
  self.assertNotEqual(result.returncode,0);self.assertIn(b'Provider parent custody invalid',result.stderr)
 def test_bound_child_killed_when_parent_is_killed(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/'child-pid'
   code="import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c',\"from shadify_api.musicbrainz import bind_parent; import os,time; bind_parent(os.getppid()); print(os.getpid(),flush=True); time.sleep(30)\"],stdout=subprocess.PIPE,text=True); Path(sys.argv[1]).write_text(p.stdout.readline()); time.sleep(30)"
   parent=subprocess.Popen([sys.executable,'-c',code,str(path)])
   try:
    deadline=time.monotonic()+4
    while not path.exists() and time.monotonic()<deadline:time.sleep(.01)
    self.assertTrue(path.exists());child=int(path.read_text());parent.kill();parent.wait(3)
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
     try:
      if Path('/proc',str(child),'stat').read_text().split()[2]=='Z':break
     except FileNotFoundError:break
     time.sleep(.01)
    else:self.fail('Parent-death child still running')
   finally:
    if parent.poll() is None:parent.kill();parent.wait()

if __name__=='__main__':unittest.main()
