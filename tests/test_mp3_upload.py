"""Real sandboxed MP3 codec tests; synthetic audio, no live uploads or publication."""
import os,subprocess,tempfile,time,unittest
from pathlib import Path
from shadify_api.media import UploadIntent,valid_signature
from shadify_api.media_tools import MediaTools,ToolFailure,mp3_source,bytes_fd
from shadify_api.ingest_worker import WorkerLimits
from shadify_api.scanner_sandbox import runtime_inventory

class MP3FramingTests(unittest.TestCase):
 def test_intent_and_signature_accept_layer_three_only(self):
  from uuid import uuid4
  intent=UploadIntent(artist_id=uuid4(),track_id=uuid4(),asset_type='audio_original',mime='audio/mpeg',size=100)
  self.assertEqual(intent.mime,'audio/mpeg')
  for prefix in (b'ID3',bytes.fromhex('fffb9000')):self.assertTrue(valid_signature('audio/mpeg',prefix))
  for prefix in (b'fake.mp3',bytes.fromhex('fffd9000')):self.assertFalse(valid_signature('audio/mpeg',prefix))
 def test_tags_without_frames_and_truncation_rejected(self):
  for body in (b'ID3',b'ID3\x04\0\0\0\0\0\0',b'ID3\x04\0\0\x80\0\0\0',bytes.fromhex('fffb9000')+b'0'*20):
   with self.assertRaises(ToolFailure):mp3_source(body)

class RealMP3Tools(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  root=os.environ.get('SHADIFY_TEST_MEDIA_ROOT')
  if not root:raise unittest.SkipTest('Explicit read-only media closure required; no real codec proof claimed')
  cls.directory=tempfile.TemporaryDirectory(prefix='shadify-mp3-codec-')
  version=subprocess.check_output([str(Path(root)/'usr/bin/ffmpeg'),'-version']).decode().split()[2]
  cls.tools=MediaTools(bwrap='/usr/bin/bwrap',runtime_root=root,prlimit='/usr/bin/prlimit',temp_dir=cls.directory.name,version=version,limits=WorkerLimits(),runtime_sha256=runtime_inventory(root,native_tools=('ffmpeg','ffprobe')))
 @classmethod
 def tearDownClass(cls):cls.directory.cleanup()
 def source(self,options):
  return subprocess.check_output(['/usr/bin/ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','libmp3lame',*options,'-f','mp3','pipe:1'])
 def validate(self,body):
  with bytes_fd(body) as fd:return self.tools.validate(fd,'audio/mpeg',deadline=time.monotonic()+60)
 def test_real_cbr_vbr_tagged_and_untagged_mp3_validate_and_encode(self):
  for options in (['-b:a','128k','-id3v2_version','0'],['-b:a','320k','-metadata','title=Synthetic fixture'],['-q:a','4'],['-ar','22050','-b:a','64k'],['-ar','11025','-b:a','64k'],['-ar','8000','-b:a','32k']):
   with self.subTest(options=options):
    body=self.source(options);validation,extraction=self.validate(body)
    self.assertFalse(validation['source_quality']['lossless']);self.assertEqual(validation['source_quality']['codec'],'mp3')
    with bytes_fd(body) as fd:outputs=self.tools.process(fd,'audio/mpeg',validation,deadline=time.monotonic()+60)
    self.assertEqual(set(outputs),{'high','low'})
    self.assertLessEqual(mp3_source(outputs['high'][1])['bitrate_kbps'],max(32,validation['source_quality']['bitrate_kbps']))
    self.assertEqual(mp3_source(outputs['low'][1])['bitrate_kbps'],128)
    for role,(_,data) in outputs.items():self.assertGreater(len(data),100)
 def test_real_wrong_codec_truncation_and_trailing_data_rejected(self):
  valid=self.source(['-b:a','128k'])
  for body in (valid[:-10],valid+b'untrusted trailing content',self.source(['-ar','8000','-b:a','16k']),b'ID3\x04\0\0\0\0\0\0'):
   with self.assertRaises(ToolFailure):self.validate(body)
 def test_real_embedded_jpeg_cover_is_validated_and_not_a_second_audio_stream(self):
  root=Path(self.directory.name);audio=root/'source.mp3';image=root/'cover.jpg';tagged=root/'tagged.mp3'
  audio.write_bytes(self.source(['-b:a','128k']))
  subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=red:s=16x16','-frames:v','1','-y',str(image)],check=True)
  subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-i',str(audio),'-i',str(image),'-map','0:a','-map','1:v','-c','copy','-id3v2_version','3','-disposition:v','attached_pic','-y',str(tagged)],check=True)
  validation,_=self.validate(tagged.read_bytes());self.assertEqual(validation['source_quality']['codec'],'mp3')
