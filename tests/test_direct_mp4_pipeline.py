from pathlib import Path
"""Direct MP4 in disposable SQL/storage, native AV/codecs and offline metadata.
016 is applied only inside the test's freshly created schema, never live data.
"""
import hashlib,json,unittest,psycopg
from uuid import UUID
from test_ingest_real_tools import RealToolQualification
from test_mp4_upload import synthetic_video
from shadify_api.musicbrainz import snapshot,query_metadata
from shadify_api.media import MediaError

class OfflineNoMatch:
 def lookup(self,metadata,*,deadline):
  query=query_metadata(metadata)
  return {**snapshot(json.dumps({'isrc':query['isrc'],'recordings':[],'count':0}).encode(),query),'fixture':'SYNTHETIC complete offline no-match, not live MusicBrainz'}

class DirectMP4Pipeline(RealToolQualification):
 def setUp(self):
  super().setUp()
  with self.connect() as c:
   for name in ('013_release_suggestions.sql','014_artist_profile_media.sql'):
    c.execute((Path(__file__).parent/'fixtures'/name).read_text())
  self.apply('016_ingest_video_after_profile.sql');self.worker.provider=OfflineNoMatch()
 def test_real_mp4_upload_scan_review_preserved_master_and_approved_video(self):
  body=synthetic_video(self.directory.name)
  response=self.api.post('/api/media/uploads',json=dict(artist_id=str(self.artist),track_id=str(self.track),asset_type='video_original',mime='video/mp4',size=len(body)))
  self.assertEqual(response.status_code,201,response.text);dto=response.json()
  self.assertEqual(self.bytes.put(dto['upload']['url'],content=body,headers=dto['upload']['headers']).status_code,201)
  self.assertEqual(self.api.post('/api/media/uploads/'+dto['asset_id']+'/complete').status_code,200)
  source=self.repo.get(UUID(dto['asset_id']))
  self.assertEqual(self.api.post('/api/media/'+str(source.id)+'/access').status_code,409)
  uploads=self.api.get('/api/artists/'+str(self.artist)+'/uploads?track_id='+str(self.track)).json()
  self.assertEqual(next(row for row in uploads['items'] if row['asset_id']==str(source.id))['asset_type'],'video_original')
  for expected in ('validating','matching','review_required'):
   result=self.once();self.assertIsNotNone(result);self.assertEqual(result['state'],expected,result)
  self.assertEqual(self.api.post('/api/media/'+str(source.id)+'/access').status_code,409)
  path='/api/media/'+str(source.id)+'/match-review';view=self.api.get(path).json()
  self.assertTrue(view['lookup_complete']);self.assertEqual(view['candidates'],[])
  response=self.api.put(path,json=dict(expected_revision=view['revision'],selected_recording_mbid=None,reason='Synthetic MP4 fixture; complete offline lookup has no match'))
  self.assertEqual(response.status_code,200,response.text);self.assertEqual(self.once()['state'],'ready')
  roles={v['role']:v for v in self.outputs(source)};self.assertEqual(set(roles),{'master','video'})
  master=self.repo.get(roles['master']['asset_id']);video=self.repo.get(roles['video']['asset_id'])
  self.assertEqual(master.asset_type,'video_original');self.assertEqual(master.access_class,'private');self.assertEqual(master.publish_state,'draft')
  self.assertEqual(self.adapter.checksum(master.key),hashlib.sha256(body).hexdigest());self.assertEqual(video.asset_type,'video_delivery')
  with self.assertRaises(psycopg.errors.CheckViolation),self.connect() as connection:
   connection.execute("UPDATE shadify_media_assets SET access_class='free',publish_state='published' WHERE id=%s",(master.id,))
  for asset in (master,video):
   result=self.api.post('/api/media/'+str(asset.id)+'/access');self.assertEqual(result.status_code,200,result.text)
   delivered=self.bytes.get(result.json()['url']);self.assertEqual(delivered.status_code,200);self.assertEqual(delivered.headers['content-type'],'video/mp4')
  # Existing content authority attaches only approved delivery. It remains draft.
  response=self.api.post('/api/artists/'+str(self.artist)+'/videos',json={'title':'Synthetic MP4 video','track_id':str(self.track),'delivery_asset_id':str(video.id)})
  self.assertEqual(response.status_code,201,response.text);self.assertEqual(response.json()['content']['publish_state'],'draft')
  invalid=self.api.patch('/api/artists/'+str(self.artist)+'/tracks/'+str(self.track),json={'original_asset_id':str(master.id)})
  self.assertEqual(invalid.status_code,422)
  self.assertEqual(self.api.post('/api/media/'+str(source.id)+'/access').status_code,409)
  self.assertIsNone(self.once())

def load_tests(loader,tests,pattern):
 return unittest.TestSuite([DirectMP4Pipeline('test_real_mp4_upload_scan_review_preserved_master_and_approved_video')])
