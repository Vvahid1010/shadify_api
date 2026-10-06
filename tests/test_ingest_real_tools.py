"""Optional real AV/encode proof using explicitly staged read-only tool closures.
No installs, live databases, roles or profiles. Matching response is a labelled
offline no-match fixture requiring the ordinary owner review route.
"""
import math,os,struct,wave,io,time,unittest,hashlib
from pathlib import Path
from test_ingest_pipeline import PipelineTests,FixtureProvider,safe_png
from unittest.mock import patch
from shadify_api.ingest_worker import safe_path
from shadify_api.scanner_sandbox import runtime_inventory,NativeSandbox
from shadify_api.clamav_scanner import ClamAVScanner
from shadify_api.media_tools import MediaTools

FIELDS=('SHADIFY_TEST_SCANNER_ROOT','SHADIFY_TEST_SCANNER_SHA256','SHADIFY_TEST_MEDIA_ROOT',
        'SHADIFY_TEST_MEDIA_SHA256','SHADIFY_TEST_MEDIA_VERSION','SHADIFY_TEST_DEFINITIONS')
class RealToolQualification(PipelineTests):
 def setUp(self):
  if not all(os.environ.get(field) for field in FIELDS):
   self.skipTest('Real read-only native closures/signatures are not explicitly configured; no real AV/encode acceptance claimed')
  if os.geteuid()==0:raise RuntimeError('Qualification requires a non-root isolated test identity')
  scanner,scanner_sha,media,media_sha,version,definitions=(os.environ[field] for field in FIELDS)
  for root in (scanner,media):safe_path(root,directory=True,root_owned=True)
  safe_path(definitions,directory=True)
  assert runtime_inventory(scanner)==scanner_sha
  assert runtime_inventory(media,native_tools=('ffmpeg','ffprobe'))==media_sha
  super().setUp() # ALWAYS creates its own isolated PG schema/root and synthetic authority.
  self.worker.scanner=ClamAVScanner(executable=Path(scanner)/'usr/bin/clamscan',prlimit='/usr/bin/prlimit',definitions=definitions,
      temp_dir=self.directory.name,engine_version='1.5.4',limits=self.worker.limits,sandbox=NativeSandbox(bwrap='/usr/bin/bwrap',runtime_root=scanner))
  self.worker.tools=MediaTools(bwrap='/usr/bin/bwrap',runtime_root=media,prlimit='/usr/bin/prlimit',temp_dir=self.directory.name,
      version=version,runtime_sha256=media_sha,limits=self.worker.limits)
  self.worker.provider=FixtureProvider(ambiguous=True) # NOT a real MusicBrainz acceptance claim.
  stream=io.BytesIO()
  with wave.open(stream,'wb') as output:
   output.setnchannels(1);output.setsampwidth(2);output.setframerate(44100)
   output.writeframes(b''.join(struct.pack('<h',int(12000*math.sin(2*math.pi*440*i/44100))) for i in range(44100)))
  self.audio=stream.getvalue()
 def test_real_short_audio_cover_decode_variants_review_and_atomic_approval(self):
  source,_,_=self.upload()
  self.assertEqual([self.once()['state'] for _ in range(3)],['validating','matching','review_required'])
  path='/api/media/'+str(source.id)+'/match-review';view=self.api.get(path).json()
  response=self.api.put(path,json=dict(expected_revision=view['revision'],selected_recording_mbid=None,reason='Locally generated sine proof; explicit fixture lookup has no match'))
  self.assertEqual(response.status_code,200,response.text);self.assertEqual(self.once()['state'],'ready')
  roles={v['role']:v for v in self.outputs(source)}
  self.assertEqual(set(roles),{'master','high','low'})
  master=self.repo.get(roles['master']['asset_id'])
  self.assertEqual(self.adapter.checksum(master.key),hashlib.sha256(self.audio).hexdigest())
  with patch('test_ingest_foundation.PNG',safe_png()):cover,_,_=self.upload(cover=True)
  self.assertEqual([self.once()['state'] for _ in range(4)],['validating','matching','processing','ready'])
  self.assertEqual({v['role'] for v in self.outputs(cover)},{'master','cover'})
  self.assertEqual(self.api.post('/api/media/'+str(source.id)+'/access').status_code,409)
  self.assertIsNone(self.once())

def load_tests(loader,tests,pattern):
 return unittest.TestSuite([RealToolQualification('test_real_short_audio_cover_decode_variants_review_and_atomic_approval')])
