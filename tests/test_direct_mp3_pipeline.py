"""Direct MP3 real SQL/HTTP + native AV/codec proof; fixture identities/metadata only."""
import hashlib,subprocess,unittest
from uuid import UUID
from test_ingest_real_tools import RealToolQualification

class DirectMP3Pipeline(RealToolQualification):
 def test_direct_mp3_quarantine_real_scan_review_master_and_private_variants(self):
  body=subprocess.check_output(['/usr/bin/ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','libmp3lame','-b:a','128k','-metadata','title=Synthetic direct MP3','-metadata','ISRC=USAAA2600001','-f','mp3','pipe:1'])
  response=self.api.post('/api/media/uploads',json=dict(artist_id=str(self.artist),track_id=str(self.track),asset_type='audio_original',mime='audio/mpeg',size=len(body)))
  self.assertEqual(response.status_code,201,response.text);dto=response.json()
  self.assertEqual(self.bytes.put(dto['upload']['url'],content=body,headers=dto['upload']['headers']).status_code,201)
  self.assertEqual(self.api.post('/api/media/uploads/'+dto['asset_id']+'/complete').status_code,200)
  source=self.repo.get(UUID(dto['asset_id']))
  self.assertEqual(self.api.post('/api/media/'+str(source.id)+'/access').status_code,409)
  for expected in ('validating','matching','review_required'):
   result=self.once();self.assertIsNotNone(result);self.assertEqual(result['state'],expected,result)
  self.assertEqual(self.api.post('/api/media/'+str(source.id)+'/access').status_code,409)
  path='/api/media/'+str(source.id)+'/match-review';view=self.api.get(path).json()
  response=self.api.put(path,json=dict(expected_revision=view['revision'],selected_recording_mbid=None,reason='Synthetic direct MP3 qualification; offline metadata fixture has no match'))
  self.assertEqual(response.status_code,200,response.text)
  self.assertEqual(self.once()['state'],'ready')
  roles={v['role']:v for v in self.outputs(source)};self.assertEqual(set(roles),{'master','high','low'})
  master=self.repo.get(roles['master']['asset_id']);self.assertEqual(master.mime,'audio/mpeg')
  self.assertEqual(self.adapter.checksum(master.key),hashlib.sha256(body).hexdigest())
  attached=self.api.patch('/api/artists/'+str(self.artist)+'/tracks/'+str(self.track),json={'original_asset_id':str(master.id)})
  self.assertEqual(attached.status_code,200,attached.text)
  for role in roles:
   result=self.api.post('/api/media/'+str(roles[role]['asset_id'])+'/access')
   self.assertEqual(result.status_code,200,result.text)
   delivered=self.bytes.get(result.json()['url']);self.assertEqual(delivered.status_code,200)
   self.assertEqual(delivered.headers['content-type'],'audio/mpeg')
  self.assertEqual(self.api.post('/api/media/'+str(source.id)+'/access').status_code,409)
  self.assertIsNone(self.once())

def load_tests(loader,tests,pattern):
 return unittest.TestSuite([DirectMP3Pipeline('test_direct_mp3_quarantine_real_scan_review_master_and_private_variants')])
