"""Real process crashes/private filesystem; provider network is mocked, never used."""
import json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from shadify_api.musicbrainz import MusicBrainzProvider,ProviderUnavailable

QUERY=dict(title='Synthetic rate fixture',artist='Fixture',isrc='USAAA2600001',duration_ms=1000)
AGENT='Shadify/test (https://shadify.org)'
CHILD=r"""
import json,os,stat,sys,time
from pathlib import Path
from unittest.mock import patch
from shadify_api.musicbrainz import MusicBrainzProvider
directory=Path(sys.argv[1]);stage=sys.argv[2]
write,fsync,replace=os.write,os.fsync,os.replace
def crash_write(fd,data):
 if stage=='partial_write':
  write(fd,data[:8]);os._exit(71)
 return write(fd,data)
def crash_fsync(fd):
 fsync(fd)
 if stage==('directory_fsync' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file_fsync'):os._exit(71)
def crash_replace(source,target):
 replace(source,target)
 if stage=='replace':os._exit(71)
def forbidden_request(*args,**kwargs):
 (directory/'unexpected-request').write_text('Request started before crash')
 raise RuntimeError('No actual network permitted')
provider=MusicBrainzProvider(state_dir=directory,user_agent='Shadify/test (https://shadify.org)')
with patch('shadify_api.musicbrainz.os.write',side_effect=crash_write),patch('shadify_api.musicbrainz.os.fsync',side_effect=crash_fsync),patch('shadify_api.musicbrainz.os.replace',side_effect=crash_replace),patch('shadify_api.musicbrainz.run_bounded',side_effect=forbidden_request):
 provider.lookup(dict(title='Synthetic rate fixture',artist='Fixture',isrc='USAAA2600001',duration_ms=1000),deadline=time.monotonic()+5)
raise AssertionError('Crash hook was not reached')
"""

class RateCrashTests(unittest.TestCase):
 def test_atomic_reservation_survives_each_process_crash_and_restart_preserves_spacing(self):
  for stage in ('partial_write','file_fsync','replace','directory_fsync'):
   with self.subTest(stage=stage),tempfile.TemporaryDirectory(prefix='shadify-rate-crash-') as directory:
    path=Path(directory)/'musicbrainz-rate.json'
    prior=dict(boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),monotonic=time.monotonic())
    path.write_text(json.dumps(prior));path.chmod(0o600)
    before=path.read_bytes()
    child=subprocess.run([sys.executable,'-c',CHILD,directory,stage],timeout=8,capture_output=True)
    self.assertEqual(child.returncode,71,child.stderr.decode())
    self.assertFalse((Path(directory)/'unexpected-request').exists())
    saved=json.loads(path.read_bytes());self.assertEqual(saved['boot'],prior['boot'])
    self.assertEqual(path.stat().st_mode&0o777,0o600)
    if stage in ('partial_write','file_fsync'):self.assertEqual(path.read_bytes(),before)
    else:self.assertGreater(saved['monotonic'],prior['monotonic'])
    started=[]
    def request(*args,**kwargs):
     started.append(time.monotonic());return 0,b'{"complete":true}',b''
    with patch('shadify_api.musicbrainz.run_bounded',side_effect=request):
     result=MusicBrainzProvider(state_dir=directory,user_agent=AGENT).lookup(QUERY,deadline=time.monotonic()+5)
    self.assertTrue(result['complete']);self.assertEqual(len(started),1)
    self.assertGreaterEqual(started[0]-saved['monotonic'],1.1)
    self.assertGreaterEqual(started[0]-prior['monotonic'],1.1)
    self.assertGreaterEqual(json.loads(path.read_bytes())['monotonic'],saved['monotonic'])
 def test_first_reservation_partial_write_crash_does_not_poison_absent_state(self):
  with tempfile.TemporaryDirectory(prefix='shadify-rate-first-') as directory:
   path=Path(directory)/'musicbrainz-rate.json'
   child=subprocess.run([sys.executable,'-c',CHILD,directory,'partial_write'],timeout=5,capture_output=True)
   self.assertEqual(child.returncode,71,child.stderr.decode());self.assertFalse(path.exists())
   with patch('shadify_api.musicbrainz.run_bounded',return_value=(0,b'{"complete":true}',b'')) as request:
    self.assertTrue(MusicBrainzProvider(state_dir=directory,user_agent=AGENT).lookup(QUERY,deadline=time.monotonic()+5)['complete'])
   request.assert_called_once();self.assertIsInstance(json.loads(path.read_bytes())['monotonic'],float)
 def test_short_writes_are_completed_and_failed_directory_sync_starts_no_request(self):
  with tempfile.TemporaryDirectory(prefix='shadify-rate-short-') as directory:
   provider=MusicBrainzProvider(state_dir=directory,user_agent=AGENT);write=os.write
   with patch('shadify_api.musicbrainz.os.write',side_effect=lambda fd,data:write(fd,data[:3])),patch('shadify_api.musicbrainz.run_bounded',return_value=(0,b'{"complete":true}',b'')):
    self.assertTrue(provider.lookup(QUERY,deadline=time.monotonic()+5)['complete'])
   path=Path(directory)/'musicbrainz-rate.json';self.assertIsInstance(json.loads(path.read_bytes())['monotonic'],float)
   fsync=os.fsync
   def fail_directory(fd):
    import stat
    if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError('Synthetic directory sync failure')
    return fsync(fd)
   with patch('shadify_api.musicbrainz.os.fsync',side_effect=fail_directory),patch('shadify_api.musicbrainz.run_bounded') as request:
    with self.assertRaises(ProviderUnavailable):provider.lookup(QUERY,deadline=time.monotonic()+5)
   request.assert_not_called();self.assertIsInstance(json.loads(path.read_bytes())['monotonic'],float)
   self.assertEqual(list(Path(directory).glob('.musicbrainz-rate-*')),[])

if __name__=='__main__':unittest.main()
