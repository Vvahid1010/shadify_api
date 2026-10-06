"""Real subprocess/PG mechanics with synthetic ClamAV protocol; NOT AV proof."""
import hashlib,multiprocessing,os,subprocess,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import patch
import psycopg
from shadify_api.clamav_scanner import ClamAVScanner,run_bounded,EvidenceInvalid
from shadify_api.ingest_worker import IngestWorker,WorkerLimits,WorkerConfig,host_lock,safe_path
from shadify_api.ingest_scanner import ScanVerdict,scan_input
from shadify_api.media import MediaError
from test_artist_postgres import ArtistPostgresCase
from test_ingest_foundation import IngestFoundationTests,FixtureScanner

FAKE="""#!/usr/bin/python3
import os,sys,time,subprocess
from pathlib import Path
mode=Path(__file__).with_suffix('.mode').read_text()
if '--version' in sys.argv:
 print('ClamAV 1.4.3/27111/Fri Oct 2 10:00:00 2026');sys.exit(0)
data=os.read(0,1) if mode=='partial' else sys.stdin.buffer.read()
Path(next(v.split('=',1)[1] for v in sys.argv if v.startswith('--tempdir=')),'synthetic-residue').write_bytes(b'SYNTHETIC SCAN SCRATCH')
if mode=='timeout':time.sleep(10)
if mode=='flood':print('X'*100000);sys.exit(0)
if mode=='descendant':
 child=subprocess.Popen(['/usr/bin/python3','-c','import time;time.sleep(10)'])
 Path(__file__).with_suffix('.pid').write_text(str(child.pid))
if mode=='change_defs':
 with (Path(next(v.split('=',1)[1] for v in sys.argv if v.startswith('--database=')))/'daily.cvd').open('ab') as f:f.write(b'changed')
infected=mode in ('infected','multi','limit')
print('stdin: '+('SYNTHETIC.Test FOUND' if mode in ('infected','multi') else 'Heuristics.Limits.Exceeded FOUND' if mode=='limit' else 'OK'))
if mode=='multi':print('stdin: SYNTHETIC.Other FOUND')
if mode=='warning':print('WARNING: skipped input',file=sys.stderr)
if mode=='empty':sys.exit(0)
print('----------- SCAN SUMMARY -----------')
print('Known viruses: 100')
print('Engine version: 1.4.3')
print('Scanned directories: 0')
print('Scanned files: '+('0' if mode=='skipped' else '1'))
print('Infected files: '+('1' if infected else '0'))
print('Data scanned: 0.01 MB')
print('Data read: 0.01 MB (ratio 1.00:1)')
print('Time: 0.01 sec (0 m 0 s)')
sys.exit(1 if infected else 0)
"""

class ClamAVAdapterTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='shadify-clamav-interface-');self.root=Path(self.tmp.name)
  self.executable=self.root/'synthetic_clamscan';self.executable.write_text(FAKE);self.executable.chmod(0o700)
  self.mode=self.executable.with_suffix('.mode');self.mode.write_text('clean')
  self.definitions=self.root/'definitions';self.definitions.mkdir()
  for name in ('main.cvd','daily.cvd'):
   (self.definitions/name).write_bytes(('ClamAV-VDB:Fri Oct 2 10-00 +0000 2026:27111:100:90:md5:signature:builder:1').encode().ljust(512,b' ')+b'SYNTHETIC DEFINITIONS NOT A REAL DB')
  self.input=self.root/'input';self.input.write_bytes(b'SYNTHETIC SAFE INPUT'*100)
  self.fd=os.open(self.input,os.O_RDONLY);self.digest=hashlib.sha256(self.input.read_bytes()).hexdigest()
  self.limits=WorkerLimits(wall_seconds=2,cpu_seconds=2,lease_seconds=122)
  self.scanner=ClamAVScanner(executable=self.executable,prlimit='/usr/bin/prlimit',definitions=self.definitions,
      temp_dir=self.root,engine_version='1.4.3',limits=self.limits)
 def tearDown(self):os.close(self.fd);self.tmp.cleanup()
 def test_real_runner_synthetic_clean_requires_full_input_and_bound_versions(self):
  result=self.scanner.scan(self.fd,sha256=self.digest)
  self.assertEqual(result.verdict,'clean');self.assertTrue(result.full_coverage)
  self.assertEqual(list(self.root.glob('shadify-scan-*')),[])
  self.assertEqual(result.engine_version,'1.4.3');self.assertTrue(result.definitions_version.startswith('official-sha256:'))
 def test_safe_synthetic_infected_report_is_distinct_from_unknown(self):
  for mode in ('infected','multi'):
   self.mode.write_text(mode);result=self.scanner.scan(self.fd,sha256=self.digest)
   self.assertEqual(result.verdict,'infected');self.assertTrue(result.full_coverage)
 def test_exit_zero_partial_skipped_empty_warning_or_limit_never_clean(self):
  for mode in ('partial','skipped','empty','warning','limit'):
   with self.subTest(mode=mode):
    self.mode.write_text(mode);result=self.scanner.scan(self.fd,sha256=self.digest)
    self.assertEqual(result.verdict,'unknown');self.assertFalse(result.full_coverage)
    self.assertEqual(result.error_code,'scanner_evidence_invalid')
 def test_timeout_and_output_flood_are_bounded_and_unknown(self):
  for mode,expected in [('timeout','scanner_timeout'),('flood','scanner_evidence_invalid')]:
   self.mode.write_text(mode);started=time.monotonic();result=self.scanner.scan(self.fd,sha256=self.digest)
   self.assertLess(time.monotonic()-started,4);self.assertEqual(result.error_code,expected);self.assertEqual(result.verdict,'unknown')
   self.assertEqual(list(self.root.glob('shadify-scan-*')),[])
 def test_descendant_pipe_timeout_kills_entire_process_group(self):
  self.mode.write_text('descendant');result=self.scanner.scan(self.fd,sha256=self.digest)
  self.assertEqual(result.error_code,'scanner_timeout')
  pid=int(self.executable.with_suffix('.pid').read_text());status=Path('/proc')/str(pid)/'stat'
  def running():
   try:return status.read_text().split()[2]!='Z'
   except FileNotFoundError:return False
  deadline=time.monotonic()+1
  while running() and time.monotonic()<deadline:time.sleep(.01)
  self.assertFalse(running())
 def test_definitions_change_missing_or_symlink_never_clean(self):
  self.mode.write_text('change_defs');self.assertEqual(self.scanner.scan(self.fd,sha256=self.digest).error_code,'scanner_evidence_invalid')
  self.mode.write_text('clean');daily=self.definitions/'daily.cvd';daily.unlink()
  self.assertEqual(self.scanner.scan(self.fd,sha256=self.digest).error_code,'scanner_unavailable')
  daily.symlink_to(self.definitions/'main.cvd');self.assertEqual(self.scanner.scan(self.fd,sha256=self.digest).verdict,'unknown')
 def test_missing_binary_and_version_mismatch_are_unknown(self):
  self.scanner.executable=str(self.root/'absent');self.assertEqual(self.scanner.scan(self.fd,sha256=self.digest).verdict,'unknown')
  self.scanner.executable=str(self.executable);self.scanner.engine_version='1.4.99'
  self.assertEqual(self.scanner.scan(self.fd,sha256=self.digest).error_code,'scanner_evidence_invalid')
 def test_host_lock_survives_parent_descriptor_close_while_child_runs(self):
  state=self.root/'state';state.mkdir(mode=0o700)
  with host_lock(state) as lock_fd:
   child=subprocess.Popen(['/usr/bin/python3','-c','import time;time.sleep(10)'],pass_fds=(lock_fd,))
  try:
   with self.assertRaises(RuntimeError):
    with host_lock(state):pass
  finally:child.terminate();child.wait()
  with host_lock(state):pass
 def test_config_limits_unknown_fields_and_unsafe_paths_are_rejected(self):
  for values in ({'wall_seconds':181},{'lease_seconds':120},{'max_scan_bytes':1},{'concurrency':2}):
   with self.assertRaises(ValueError):WorkerLimits(**values)
  link=self.root/'link';link.symlink_to(self.input)
  with self.assertRaises(OSError):safe_path(link)
  with self.assertRaises(ValueError):safe_path(self.executable,root_owned=True)

class WorkerLeaseTests(IngestFoundationTests):
 setUp=IngestFoundationTests.setUp
 tearDown=IngestFoundationTests.tearDown
 asset=IngestFoundationTests.asset
 upload=IngestFoundationTests.upload
 metadata=IngestFoundationTests.metadata
 job=IngestFoundationTests.job
 def worker(self,scanner=None,*,lease=360,retry=30,connect=None):
  return IngestWorker(connect=connect or self.connect,adapters={'local':self.adapter},scanner=scanner,
      limits=SimpleNamespace(lease_seconds=lease,retry_seconds=retry))
 def test_oversized_worker_input_is_bounded_before_hash_or_scanner(self):
  self.upload();worker=self.worker(FixtureScanner());worker.limits.max_input_bytes=1
  with patch('shadify_api.ingest_scanner.fd_digest',side_effect=AssertionError('must not hash oversize')):
   result=worker.run_once()
  self.assertEqual(result['state'],'uploaded');self.assertEqual(result['last_error'],'scanner_evidence_invalid')
 def test_input_growth_during_hash_rejects_without_unbounded_read(self):
  asset,_,_=self.upload();job=self.job(asset);read=os.read;grew=[False]
  def growing(fd,size):
   if not grew[0]:
    grew[0]=True
    with (Path(self.directory.name)/asset.key).open('ab') as stream:stream.write(b'SYNTHETIC GROWTH')
   return read(fd,size)
  with patch('shadify_api.ingest_scanner.os.read',side_effect=growing):result=self.worker(FixtureScanner()).run_once()
  self.assertEqual(result['state'],'rejected');self.assertEqual(result['last_error'],'bytes_changed')
 def test_expired_shared_deadline_never_calls_scanner(self):
  asset,_,_=self.upload();job=self.job(asset)
  with patch.object(FixtureScanner,'scan',side_effect=AssertionError('must not scan after deadline')):
   verdict=scan_input(asset,self.adapter.checksum(asset.key),adapters={'local':self.adapter},scanner=FixtureScanner(),deadline=time.monotonic()-1)
  self.assertEqual(verdict.verdict,'unknown');self.assertEqual(verdict.error_code,'scanner_timeout')
 def test_real_subprocess_adapter_result_is_committed_with_durable_attempt(self):
  fixture=ClamAVAdapterTests();fixture.setUp()
  try:
   asset,_,_=self.upload();result=self.worker(fixture.scanner).run_once()
   self.assertEqual(result['state'],'validating');self.assertFalse(result['approved'])
   with self.connect() as c:
    row=c.execute('SELECT input_sha256,engine_version,definitions_version,full_coverage FROM shadify_ingest_scans').fetchone()
    self.assertEqual(row[0],self.adapter.checksum(asset.key));self.assertEqual(row[1],'1.4.3')
    self.assertTrue(row[2].startswith('official-sha256:'));self.assertTrue(row[3])
  finally:fixture.tearDown()
 def test_actual_worker_process_kill_releases_exclusion_and_expired_attempt_recovers(self):
  self.upload();context=multiprocessing.get_context('fork');parent,child=context.Pipe()
  state=Path(self.directory.name)/'worker-state';state.mkdir(mode=0o700)
  def run():
   parent.close()
   with host_lock(state),self.worker(lease=.3).session() as session:
    claim=self.worker(lease=.3).claim(session);child.send(str(claim[1]['id']))
    time.sleep(10)
  process=context.Process(target=run);process.start();child.close()
  try:
   self.assertTrue(parent.poll(3));parent.recv();process.kill();process.join(3)
   self.assertFalse(process.is_alive());time.sleep(.35)
   with host_lock(state):
    self.assertEqual(self.worker(FixtureScanner()).run_once()['state'],'validating')
   with self.connect() as c:
    self.assertEqual(c.execute('SELECT outcome FROM shadify_ingest_attempts ORDER BY claimed_revision').fetchall(),[('abandoned',),('completed',)])
  finally:
   parent.close()
   if process.is_alive():process.kill();process.join()
 def test_worker_commits_clean_scan_attempt_and_validating_not_ready(self):
  asset,_,_=self.upload();result=self.worker(FixtureScanner()).run_once()
  self.assertEqual(result['state'],'validating');self.assertFalse(result['approved'])
  with self.connect() as c:
   self.assertEqual(c.execute("SELECT outcome,finished_at IS NOT NULL FROM shadify_ingest_attempts").fetchone(),('completed',True))
   self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_approvals').fetchone()[0],0)
 def test_worker_missing_scanner_retry_wait_and_safe_infected_are_distinct(self):
  asset,_,_=self.upload();result=self.worker().run_once();self.assertEqual(result['last_error'],'scanner_unavailable')
  self.assertTrue(result['retryable']);self.assertIsNone(self.worker().run_once())
  result=self.worker(FixtureScanner('infected'),retry=0).run_once();self.assertEqual(result['state'],'rejected')
  self.assertEqual(result['last_error'],'malware_detected');self.assertFalse(result['retryable'])
 def test_two_repeated_old_timeouts_cannot_starve_fresh_job(self):
  first,_,_=self.upload();second,_,_=self.upload()
  first_job=self.job(first)['job_id'];second_job=self.job(second)['job_id']
  class SlowTimeout:
   def scan(self,fd,*,sha256,deadline=None):
    # Scale180s scan/30s retry to60ms/10ms; real DB clocks/transactions remain.
    time.sleep(.06);raise TimeoutError('synthetic repeatable scanner timeout')
  worker=self.worker(SlowTimeout(),retry=.01)
  seen=[worker.run_once()['job_id']]
  self.assertEqual(seen,[first_job])
  fresh,_,_=self.upload();fresh_job=self.job(fresh)['job_id']
  for _ in range(3):seen.append(worker.run_once()['job_id'])
  self.assertEqual(seen,[first_job,second_job,first_job,fresh_job])
  with self.connect() as c:
   self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_attempts WHERE job_id=%s',(UUID(fresh_job),)).fetchone()[0],1)
   self.assertEqual(c.execute('SELECT state,last_error FROM shadify_ingest_jobs WHERE id=%s',(UUID(fresh_job),)).fetchone(),('uploaded','scanner_timeout'))

 def test_same_job_table_excludes_worker_with_different_leading_search_path(self):
  from psycopg import sql
  from uuid import uuid4
  self.upload();shadow='shadow_'+uuid4().hex
  with self.connect() as c:c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(shadow)))
  def connect_shadow():
   return psycopg.connect(**self.server.connection,options='-c search_path='+shadow+','+self.schema)
  try:
   worker=self.worker(lease=.1)
   with worker.session() as session:
    worker.claim(session);time.sleep(.2)
    self.assertIsNone(self.worker(FixtureScanner(),connect=connect_shadow).run_once())
  finally:
   with self.connect() as c:c.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(shadow)))
 def test_live_worker_excludes_second_even_after_lease_expiry(self):
  self.upload();worker=self.worker(lease=.1)
  with worker.session() as session:
   claim=worker.claim(session);time.sleep(.2)
   self.assertIsNone(self.worker(lease=.1).run_once())
   with self.assertRaises(MediaError):worker.finish(session,claim,ScanVerdict('unknown',claim[0]['input_sha256'],error_code='scanner_error'))
 def test_restart_reclaims_expired_attempt_and_stale_result_cannot_commit(self):
  self.upload();worker=self.worker(lease=.1)
  with worker.session() as session:old=worker.claim(session)
  time.sleep(.2);new_worker=self.worker(FixtureScanner())
  with new_worker.session() as session:
   new=new_worker.claim(session);self.assertGreater(new[0]['revision'],old[0]['revision'])
   with self.assertRaises(MediaError):new_worker.finish(session,old,ScanVerdict('unknown',old[0]['input_sha256'],error_code='scanner_error'))
   new_worker.process(session,new)
  with self.connect() as c:
   self.assertEqual(c.execute('SELECT outcome FROM shadify_ingest_attempts ORDER BY claimed_revision').fetchall(),[('abandoned',),('completed',)])
   self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_scans').fetchone()[0],1)
 def test_committed_response_loss_does_not_rescan_or_duplicate_evidence(self):
  self.upload();self.worker(FixtureScanner()).run_once();self.assertIsNone(self.worker(FixtureScanner()).run_once())
  with self.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_attempts').fetchone()[0],1)
 def test_unleased_historical_scanning_never_guesses_recovery(self):
  asset,_,_=self.upload();job=self.job(asset)
  with self.connect() as c:c.execute("UPDATE shadify_ingest_jobs SET state='scanning',revision=revision+1 WHERE id=%s",(UUID(job['job_id']),))
  self.assertIsNone(self.worker(FixtureScanner()).run_once())
 def test_finished_attempt_identity_and_journal_cannot_rewrite(self):
  self.upload();self.worker(FixtureScanner()).run_once()
  for query in ("UPDATE shadify_ingest_attempts SET outcome='abandoned'",'DELETE FROM shadify_ingest_attempts'):
   with self.assertRaises(psycopg.Error):
    with self.connect() as c:c.execute(query)
 def test_scan_or_attempt_failure_rolls_back_result_and_recovery_keeps_evidence_atomic(self):
  self.upload();worker=self.worker(FixtureScanner())
  with worker.session() as session:
   claim=worker.claim(session)
   with self.connect() as c:c.execute("CREATE FUNCTION fixture_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic'; END $$; CREATE TRIGGER fixture_failure BEFORE INSERT ON shadify_ingest_scans FOR EACH ROW EXECUTE FUNCTION fixture_failure()")
   with self.assertRaises(psycopg.Error):worker.process(session,claim)
  with self.connect() as c:
   self.assertEqual(c.execute('SELECT count(*) FROM shadify_ingest_scans').fetchone()[0],0)
   self.assertIsNone(c.execute('SELECT finished_at FROM shadify_ingest_attempts').fetchone()[0])
 def test_worker_minimal_role_can_scan_and_cannot_approve_or_edit_artists(self):
  from psycopg import sql
  from uuid import uuid4
  self.upload();role='worker_fixture_'+uuid4().hex
  with self.connect() as c:
   c.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
   c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(self.schema),sql.Identifier(role)))
   for privileges,tables in [('SELECT',['shadify_media_assets']),('SELECT,UPDATE(state,revision,last_error,updated_at)',['shadify_ingest_jobs']),('SELECT,INSERT',['shadify_ingest_scans']),('SELECT,INSERT,UPDATE(finished_at,outcome)',['shadify_ingest_attempts'])]:
    c.execute(sql.SQL('GRANT '+privileges+' ON '+','.join(tables)+' TO {}').format(sql.Identifier(role)))
  def connect():
   c=self.connect();c.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)));return c
  try:
   result=self.worker(FixtureScanner(),connect=connect).run_once();self.assertEqual(result['state'],'validating')
   for query in ('INSERT INTO shadify_ingest_approvals DEFAULT VALUES',"UPDATE shadify_artists SET name='wrong'",'DELETE FROM shadify_ingest_scans'):
    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
     with connect() as c:c.execute(query)
  finally:
   with self.connect() as c:c.execute(sql.SQL('DROP OWNED BY {}; DROP ROLE {}').format(sql.Identifier(role),sql.Identifier(role)))

_set_up=WorkerLeaseTests.setUp
def worker_set_up(self):
 _set_up(self);self.apply('010_ingest_attempts.sql')
WorkerLeaseTests.setUp=worker_set_up

def load_tests(loader,tests,pattern):
 suite=unittest.TestSuite([loader.loadTestsFromTestCase(ClamAVAdapterTests)])
 suite.addTests(WorkerLeaseTests(name) for name in WorkerLeaseTests.__dict__ if name.startswith('test_'))
 return suite
