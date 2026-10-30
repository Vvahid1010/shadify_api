import tempfile,subprocess,os,time,hashlib,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from shadify_api.client_avif import validate
from shadify_api.media_tools import MediaTools,bytes_fd,ToolFailure
from shadify_api.profile_media import ProfileUploadIntent
from test_profile_image_reconciliation import ProfileImageTests
from test_ingest_pipeline import safe_png

PACKAGE='/var/lib/chapary-local/mizfood-dev/spa-media-v1/sharp-0.35.3'
def generated(path,metadata=False):
 code="const path=require('node:path');const {createRequire}=require('node:module');const sharp=createRequire(path.join(process.argv[1],'package.json'))('sharp');let s=sharp({create:{width:16,height:16,channels:3,background:'#208030'}});if(process.argv[3]==='metadata')s=s.withExif({IFD0:{Artist:'synthetic'}});s.avif({quality:60}).toFile(process.argv[2]);"
 subprocess.run(['/usr/bin/node','-e',code,PACKAGE,str(path),'metadata' if metadata else 'valid'],check=True,capture_output=True,timeout=20)
 return path.read_bytes()

class ClientAvifTests(unittest.TestCase):
 def test_real_generated_structure_and_metadata_rejection(self):
  with tempfile.TemporaryDirectory() as folder:
   data=generated(Path(folder)/'valid.avif');self.assertEqual(validate(data),data)
   for bad in (data[:-5],data+b'bad',b'bad',data+b'x'*1048576):
    with self.assertRaises(ValueError):validate(bad)
   with self.assertRaises(ValueError):validate(generated(Path(folder)/'meta.avif',True))
 def test_empty_brand_and_item_boxes_reject_normally(self):
  box=lambda kind,data:(len(data)+8).to_bytes(4,'big')+kind+data
  for brand,items in ((b'',b''),(b'avif'+b'\0'*4,b''),(b'avif'+b'\0'*4,b'\0'*4+b'\0\1'+box(b'infe',b''))):
   meta=b'\0'*4+b''.join(box(k,b'') for k in (b'hdlr',b'pitm',b'iloc'))+box(b'iinf',items)+box(b'iprp',b'')
   data=box(b'ftyp',brand)+box(b'meta',meta)+box(b'mdat',b'x')
   with self.assertRaises(ValueError):validate(data)
 def test_profile_avif_roles_preserve_exact_bytes_without_encoder(self):
  with tempfile.TemporaryDirectory() as folder:
   data=generated(Path(folder)/'valid.avif');tools=object.__new__(MediaTools)
   tools.run=lambda *a,**k:(_ for _ in ()).throw(AssertionError('AVIF reencode forbidden'))
   for purpose in ('avatar','header'):
    with bytes_fd(data) as fd:
     out=tools.profile_outputs(fd,'image/avif',{'source_quality':{'width':16,'height':16}},purpose,deadline=time.monotonic()+2,roles={'cover_avif','cover_avif_small'})
    self.assertEqual({hashlib.sha256(v[1]).hexdigest() for v in out.values()},{hashlib.sha256(data).hexdigest()})
   with bytes_fd(data) as fd:
    with self.assertRaises(ToolFailure):tools.profile_outputs(fd,'image/avif',{'source_quality':{'width':513,'height':16}},'avatar',deadline=time.monotonic()+2,roles={'cover_avif'})

class ClientProfilePGTests(ProfileImageTests):
 def test_avif_upload_variant_sql_and_truthful_checks(self):
  self.apply('016_ingest_video_after_profile.sql','022_profile_image_variants.sql','024_client_avif_images.sql')
  with tempfile.TemporaryDirectory() as folder:data=generated(Path(folder)/'valid.avif')
  base_validate=self.tools.validate
  def validation(fd,mime,**kwargs):
   if mime!='image/avif':return base_validate(fd,mime,**kwargs)
   os.lseek(fd,0,os.SEEK_SET);validate(os.read(fd,1048577))
   return {'passed':True,'fixture':'SYNTHETIC validation; real decode separate','source_quality':{'width':16,'height':16,'frames':1}}, {'complete':True,'metadata':{}}
  self.tools.validate=validation
  def process(fd,mime,validation,*,deadline,roles=None,profile_purpose=None):
   self.assertEqual(profile_purpose,'avatar');return {key:('image/avif',data) if 'avif' in key else ('image/png',safe_png()) for key in ('cover','cover_small','cover_avif','cover_avif_small')}
  self.tools.process=process
  response=self.api.post('/api/artists/'+str(self.artist)+'/profile-media/uploads',json={'purpose':'avatar','mime':'image/avif','size':len(data)})
  self.assertEqual(response.status_code,201,response.text);dto=response.json()
  self.assertEqual(self.bytes.put(dto['upload']['url'],content=data,headers=dto['upload']['headers']).status_code,201)
  self.assertEqual(self.api.post('/api/media/uploads/'+dto['asset_id']+'/complete').status_code,200)
  before=self.api.get('/api/media/'+dto['asset_id']).json()['profile_checks'];self.assertFalse(before['durable_committed'])
  self.assertEqual([self.once()['state'] for _ in range(4)],['validating','matching','processing','ready'])
  after=self.api.get('/api/media/'+dto['asset_id']).json()['profile_checks'];self.assertEqual(after,dict(scan='passed',validation='passed',optimization='passed',durable_committed=True))
  from authority_fixture import admitted_actor
  with self.connect() as c:revision=c.execute('SELECT revision FROM shadify_app_admin WHERE singleton=true').fetchone()[0]
  self.repo.set_delegate(admitted_actor('admin'),'listener',['edit_profile'],expected_revision=revision)
  self.actor='listener'
  self.assertEqual(self.api.get('/api/media/'+dto['asset_id']).status_code,200)
  self.assertIn(self.api.get('/api/media/'+str(self.legacy.id)).status_code,(403,404))

  with self.connect() as c:
   hashes=c.execute("SELECT output_sha256 FROM shadify_ingest_variants WHERE role IN ('cover_avif','cover_avif_small')").fetchall()
   self.assertEqual({h[0] for h in hashes},{hashlib.sha256(data).hexdigest()})

class ClientArtworkPGTests(ProfileImageTests):
 def test_artwork_avif_preservation_keeps_existing_audio_matching(self):
  self.apply('016_ingest_video_after_profile.sql','022_profile_image_variants.sql','024_client_avif_images.sql')
  self.audio_ready()
  with tempfile.TemporaryDirectory() as folder:data=generated(Path(folder)/'valid.avif')
  base=self.tools.validate
  def validation(fd,mime,**kw):
   if mime!='image/avif':return base(fd,mime,**kw)
   os.lseek(fd,0,os.SEEK_SET);validate(os.read(fd,1048577))
   return {'passed':True,'source_quality':{'width':16,'height':16,'frames':1}}, {'complete':True,'metadata':{}}
  self.tools.validate=validation
  self.tools.process=lambda *a,**kw:{'cover':('image/png',safe_png()),'cover_avif':('image/avif',data)}
  response=self.api.post('/api/media/uploads',json=dict(artist_id=str(self.artist),track_id=str(self.track),asset_type='artwork_original',mime='image/avif',size=len(data)))
  self.assertEqual(response.status_code,201,response.text);dto=response.json()
  self.assertEqual(self.bytes.put(dto['upload']['url'],content=data,headers=dto['upload']['headers']).status_code,201)
  self.assertEqual(self.api.post('/api/media/uploads/'+dto['asset_id']+'/complete').status_code,200)
  self.assertEqual([self.once()['state'] for _ in range(4)],['validating','matching','processing','ready'])
  with self.connect() as c:
   row=c.execute("SELECT v.output_sha256 FROM shadify_ingest_variants v JOIN shadify_ingest_jobs j ON j.id=v.job_id WHERE j.asset_id=%s AND v.role='cover_avif'",(dto['asset_id'],)).fetchone()
   self.assertEqual(row[0],hashlib.sha256(data).hexdigest())

class RealAvifDecoderTests(unittest.TestCase):
 def test_installed_isolated_decoder_valid_truncated_and_preserved_bytes(self):
  from shadify_api.scanner_sandbox import runtime_inventory
  from shadify_api.ingest_worker import WorkerLimits
  root='/var/lib/chapary-local/shadify-dev/media-runtime-wsl-v1'
  with tempfile.TemporaryDirectory() as folder:
   version=subprocess.check_output([root+'/usr/bin/ffmpeg','-version'],timeout=5).decode().split()[2]
   tools=MediaTools(bwrap='/usr/bin/bwrap',runtime_root=root,prlimit='/usr/bin/prlimit',temp_dir=folder,
    version=version,limits=WorkerLimits(),runtime_sha256=runtime_inventory(root,native_tools=('ffmpeg','ffprobe')))
   data=generated(Path(folder)/'valid.avif')
   with bytes_fd(data) as fd:
    result,_=tools.validate(fd,'image/avif',deadline=time.monotonic()+30)
    self.assertEqual(result['source_quality']['frames'],1)
    out=tools.profile_outputs(fd,'image/avif',result,'avatar',deadline=time.monotonic()+30)
   self.assertEqual(out['cover_avif'][1],data);self.assertEqual(out['cover_avif_small'][1],data)
   self.assertEqual(out['cover'][0],'image/png')
   with bytes_fd(data[:-10]) as fd:
    with self.assertRaises(ToolFailure):tools.validate(fd,'image/avif',deadline=time.monotonic()+30)

def load_tests(loader,tests,pattern):
 return unittest.TestSuite([loader.loadTestsFromTestCase(ClientAvifTests),loader.loadTestsFromTestCase(RealAvifDecoderTests),ClientProfilePGTests('test_avif_upload_variant_sql_and_truthful_checks'),ClientArtworkPGTests('test_artwork_avif_preservation_keeps_existing_audio_matching')])
