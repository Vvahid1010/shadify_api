import tempfile,subprocess,time,os,unittest,hashlib
from pathlib import Path
from shadify_api.media_tools import MediaTools,bytes_fd
from shadify_api.client_images import optimized
from shadify_api.scanner_sandbox import runtime_inventory
from shadify_api.ingest_worker import WorkerLimits
from test_profile_image_reconciliation import ProfileImageTests
from test_ingest_pipeline import safe_png
PACKAGE='/var/lib/chapary-local/mizfood-dev/spa-media-v1/sharp-0.35.3'
def generate(path,fmt):
 code="const {createRequire}=require('node:module');const path=require('node:path');const sharp=createRequire(path.join(process.argv[1],'package.json'))('sharp');sharp({create:{width:16,height:16,channels:4,background:{r:20,g:100,b:30,alpha:0.5}}}).toFormat(process.argv[3]).toFile(process.argv[2]);"
 subprocess.run(['/usr/bin/node','-e',code,PACKAGE,str(path),fmt],check=True,capture_output=True,timeout=20);return path.read_bytes()
class NativeFormats(unittest.TestCase):
 def test_four_formats_full_isolated_decode_and_unchanged_optimized_bytes(self):
  root='/var/lib/chapary-local/shadify-dev/media-runtime-wsl-v1'
  with tempfile.TemporaryDirectory() as d:
   version=subprocess.check_output([root+'/usr/bin/ffmpeg','-version'],timeout=5).decode().split()[2]
   tools=MediaTools(bwrap='/usr/bin/bwrap',runtime_root=root,prlimit='/usr/bin/prlimit',temp_dir=d,version=version,limits=WorkerLimits(),runtime_sha256=runtime_inventory(root,native_tools=('ffmpeg','ffprobe')))
   for fmt in ('avif','webp','jpeg','png'):
    with self.subTest(fmt=fmt):
     data=generate(Path(d)/('image.'+fmt),fmt);self.assertTrue(optimized(data,'image/'+fmt))
     if fmt=='jpeg':self.assertFalse(optimized(data[:-2]+b'\xff\xfe\x00\x06test'+data[-2:],'image/jpeg'))
     with bytes_fd(data) as fd:
      result,_=tools.validate(fd,'image/'+fmt,deadline=time.monotonic()+30)
      self.assertTrue(result['client_optimized']);out=tools.profile_outputs(fd,'image/'+fmt,result,'avatar',deadline=time.monotonic()+30)
     self.assertEqual(out['cover_avif'],('image/'+fmt,data));self.assertEqual(out['cover_avif_small'],('image/'+fmt,data));self.assertEqual(out['cover'][0],'image/png')
     if fmt in ('avif','webp','png'):
      image=Path(d)/'fallback.png';image.write_bytes(out['cover'][1])
      check="const {createRequire}=require('node:module');const path=require('node:path');const sharp=createRequire(path.join(process.argv[1],'package.json'))('sharp');sharp(process.argv[2]).raw().toBuffer({resolveWithObject:true}).then(x=>{if(x.info.channels!==4||x.data[3]>=255)process.exit(2)});"
      subprocess.run(['/usr/bin/node','-e',check,PACKAGE,str(image)],check=True,capture_output=True,timeout=10)
class PGFormats(ProfileImageTests):
 def test_four_formats_profile_approval_same_bytes_and_scope(self):
  self.apply('022_profile_image_variants.sql','024_client_avif_images.sql')
  with tempfile.TemporaryDirectory() as d:
   for fmt in ('avif','webp','jpeg','png'):
    data=generate(Path(d)/('image.'+fmt),fmt);mime='image/'+fmt
    self.tools.validate=lambda *a,**kw:({'passed':True,'client_optimized':True,'source_quality':{'width':16,'height':16,'frames':1}},{'complete':True,'metadata':{}})
    self.tools.process=lambda *a,**kw:{role:(mime,data) if 'avif' in role else ('image/png',safe_png()) for role in ('cover','cover_small','cover_avif','cover_avif_small')}
    response=self.api.post('/api/artists/'+str(self.artist)+'/profile-media/uploads',json={'purpose':'avatar','mime':mime,'size':len(data)})
    self.assertEqual(response.status_code,201,response.text);dto=response.json()
    self.assertEqual(self.bytes.put(dto['upload']['url'],content=data,headers=dto['upload']['headers']).status_code,201)
    self.assertEqual(self.api.post('/api/media/uploads/'+dto['asset_id']+'/complete').status_code,200)
    self.assertEqual([self.once()['state'] for _ in range(4)],['validating','matching','processing','ready'])
    self.assertTrue(self.api.get('/api/media/'+dto['asset_id']).json()['profile_checks']['durable_committed'])
def load_tests(loader,tests,pattern):return unittest.TestSuite([loader.loadTestsFromTestCase(NativeFormats),PGFormats('test_four_formats_profile_approval_same_bytes_and_scope')])
