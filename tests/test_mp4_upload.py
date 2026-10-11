"""MP4 framing and real native sandbox codec tests, using generated fixtures only."""
import os,subprocess,tempfile,time,unittest,json
from pathlib import Path
from shadify_api.media import UploadIntent,valid_signature
from shadify_api.media_tools import MediaTools,ToolFailure,mp4_container,bytes_fd
from shadify_api.ingest_worker import WorkerLimits
from shadify_api.scanner_sandbox import runtime_inventory

def synthetic_video(root,*,name='source',codec='libx264',audio='aac',fps=25,faststart=False,profile=None):
 path=Path(root)/(name+'.mp4')
 subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-f','lavfi','-i',f'testsrc2=size=128x96:rate={fps}:duration=1','-f','lavfi','-i','sine=frequency=440:duration=1',
  '-c:v',codec,*(['-profile:v',profile] if profile else []),'-pix_fmt','yuv420p','-threads:v','1','-c:a',audio,'-b:a','128k','-metadata','artist=Test','-metadata','title=Synthetic MP4','-metadata','ISRC=USAAA2600001',
  *(['-movflags','+faststart'] if faststart else []),'-y',str(path)],check=True)
 return path.read_bytes()

class MP4FramingTests(unittest.TestCase):
 def test_video_intent_and_signature_are_distinct_from_audio(self):
  from uuid import uuid4
  intent=UploadIntent(artist_id=uuid4(),track_id=uuid4(),asset_type='video_original',mime='video/mp4',size=100)
  self.assertEqual(intent.asset_type,'video_original')
  self.assertTrue(valid_signature('video/mp4',b'\0\0\0\x18ftypisom'))
  self.assertFalse(valid_signature('video/mp4',b'\0\0\0\x18ftypqt  '))
 def test_incomplete_boxes_duplicate_metadata_and_trailing_bytes_rejected(self):
  def box(kind,payload):return (len(payload)+8).to_bytes(4,'big')+kind+payload
  valid=box(b'ftyp',b'isom\0\0\0\0')+box(b'moov',b'')+box(b'mdat',b'fixture')
  mp4_container(valid)
  for data in (valid[:-1],valid+b'x',valid+box(b'moov',b''),valid.replace(b'isom',b'qt  ')):
   with self.assertRaises(ToolFailure):mp4_container(data)

class RealMP4Tools(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  root=os.environ.get('SHADIFY_TEST_MEDIA_ROOT')
  if not root:raise unittest.SkipTest('Explicit read-only media closure required; no codec acceptance claimed')
  cls.directory=tempfile.TemporaryDirectory(prefix='shadify-mp4-codec-')
  version=subprocess.check_output([str(Path(root)/'usr/bin/ffmpeg'),'-version']).decode().split()[2]
  cls.tools=MediaTools(bwrap='/usr/bin/bwrap',runtime_root=root,prlimit='/usr/bin/prlimit',temp_dir=cls.directory.name,version=version,limits=WorkerLimits(),runtime_sha256=runtime_inventory(root,native_tools=('ffmpeg','ffprobe')))
 @classmethod
 def tearDownClass(cls):cls.directory.cleanup()
 def validate(self,body):
  with bytes_fd(body) as fd:return self.tools.validate(fd,'video/mp4',deadline=time.monotonic()+60)
 def test_real_tail_moov_and_faststart_h264_aac_decode_and_process(self):
  for faststart,profile,fps in ((False,'baseline',25),(True,'main',25),(False,'high',25),(True,'high',30),(False,'high',15)):
   with self.subTest(faststart=faststart,profile=profile,fps=fps):
    body=synthetic_video(self.directory.name,faststart=faststart,profile=profile,fps=fps)
    validation,extraction=self.validate(body);quality=validation['source_quality']
    self.assertEqual((quality['codec'],quality['audio_codec']),('h264','aac'));self.assertFalse(quality['lossless'])
    self.assertEqual(extraction['metadata']['artist'],'Test')
    with bytes_fd(body) as fd:outputs=self.tools.process(fd,'video/mp4',validation,deadline=time.monotonic()+60)
    self.assertEqual(set(outputs),{'video'});self.assertEqual(outputs['video'][0],'video/mp4')
    self.validate(outputs['video'][1]);self.assertNotEqual(body,outputs['video'][1])
 def test_real_other_codecs_high_frame_rate_truncation_and_extra_data_rejected(self):
  good=synthetic_video(self.directory.name)
  wrong_video=synthetic_video(self.directory.name,name='wrong-video',codec='mpeg4')
  wrong_audio=synthetic_video(self.directory.name,name='wrong-audio',audio='libmp3lame')
  fast=synthetic_video(self.directory.name,name='fast',fps=60)
  for body in (wrong_video,wrong_audio,fast,good[:-15],good+b'untrusted trailing data'):
   with self.assertRaises(ToolFailure):self.validate(body)
 def test_missing_mandatory_audio_and_extra_subtitle_stream_rejected(self):
  root=Path(self.directory.name);source=root/'source.mp4';source.write_bytes(synthetic_video(root))
  silent=root/'silent.mp4'
  subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-i',str(source),'-map','0:v','-c','copy','-y',str(silent)],check=True)
  subtitle=root/'subtitle.srt';subtitle.write_text('1\n00:00:00,000 --> 00:00:00,800\nSynthetic\n')
  extra=root/'extra.mp4'
  subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-i',str(source),'-i',str(subtitle),'-map','0','-map','1','-c','copy','-c:s','mov_text','-y',str(extra)],check=True)
  for body in (silent.read_bytes(),extra.read_bytes()):
   with self.assertRaises(ToolFailure):self.validate(body)
 def test_seekable_input_is_readonly_and_failure_never_falls_back_to_host_tools(self):
  body=synthetic_video(self.directory.name)
  with bytes_fd(body) as fd:
   result=self.tools.run('ffprobe',['-v','error','-protocol_whitelist','file','-show_entries','format=format_name','-of','json','/scratch/input'],fd,deadline=time.monotonic()+20,seekable=True)
   self.assertIn('mp4',json.loads(result)['format']['format_name'])
   with self.assertRaises(ToolFailure):self.tools.run('ffmpeg',['-nostdin','-v','error','-f','lavfi','-i','color=s=16x16:d=1','-c:v','libx264','-f','mp4','-y','/scratch/input'],fd,deadline=time.monotonic()+20,seekable=True)
   self.assertEqual(os.fstat(fd).st_size,len(body))


class MP4SandboxBoundaryTests(unittest.TestCase):
 def test_descriptor_consumed_readonly_without_host_paths_credentials_or_extra_fds(self):
  # Real bwrap namespace, synthetic reporter only; not codec qualification.
  from test_clamav_worker import fixture_runtime
  from unittest.mock import patch
  runtime=fixture_runtime()
  with tempfile.TemporaryDirectory(prefix='shadify-video-boundary-') as scratch,bytes_fd(b'SYNTHETIC OBJECT') as fd:
   reporter="""#!/usr/bin/python3
import os,sys
assert 'SYNTHETIC_DB_PASSWORD' not in os.environ
assert not os.path.exists('/etc/worker-db.json')
try:os.fstat(INPUT_FD)
except OSError:pass
else:raise RuntimeError('Input descriptor leaked to payload')
assert open('/scratch/input','rb').read()==b'SYNTHETIC OBJECT'
try:open('/scratch/input','wb')
except OSError:pass
else:raise RuntimeError('Input mount writable')
sys.stdout.write('bounded readonly descriptor data')
""".replace('INPUT_FD',str(fd))
   executable=runtime/'usr/bin/ffprobe';executable.write_text(reporter);executable.chmod(0o755)
   tools=MediaTools(bwrap='/usr/bin/bwrap',runtime_root=runtime,prlimit='/usr/bin/prlimit',temp_dir=scratch,version='SYNTHETIC',limits=WorkerLimits())
   with patch.dict(os.environ,SYNTHETIC_DB_PASSWORD='SYNTHETIC-NOT-LIVE'):
    result=tools.run('ffprobe',[],fd,deadline=time.monotonic()+10,seekable=True)
   self.assertEqual(result,b'bounded readonly descriptor data')
   tools.sandbox.bwrap='/usr/bin/false'
   with self.assertRaises(ToolFailure):tools.run('ffprobe',[],fd,deadline=time.monotonic()+10,seekable=True)
