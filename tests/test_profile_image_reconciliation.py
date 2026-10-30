"""Synthetic private PostgreSQL/HTTP image baseline tests; no real scanner claim."""
from uuid import UUID
from test_ingest_pipeline import PipelineTests,safe_png

class ProfileImageTests(PipelineTests):
 def setUp(self):
  super().setUp();self.apply('014_artist_profile_media.sql')
 def test_profile_upload_approval_and_local_ownership(self):
  body=safe_png()
  response=self.api.post('/api/artists/'+str(self.artist)+'/profile-media/uploads',json={'purpose':'avatar','mime':'image/png','size':len(body)})
  self.assertEqual(response.status_code,201,response.text);dto=response.json()
  self.assertEqual(self.bytes.put(dto['upload']['url'],content=body,headers=dto['upload']['headers']).status_code,201)
  response=self.api.post('/api/media/uploads/'+dto['asset_id']+'/complete');self.assertEqual(response.status_code,200,response.text)
  source=self.repo.get(UUID(dto['asset_id']));self.assertIsNone(source.track_id)
  self.assertEqual([self.once()['state'] for _ in range(4)],['validating','matching','processing','ready'])
  self.assertEqual(self.provider.calls,0)
  outputs=self.outputs(source);cover=next(v for v in outputs if v['role']=='cover')
  response=self.api.patch('/api/artists/'+str(self.artist),json={'avatar_asset_id':cover['asset_id']})
  self.assertEqual(response.status_code,200,response.text)
  self.actor='listener'
  denied=self.api.post('/api/artists/'+str(self.artist)+'/profile-media/uploads',json={'purpose':'avatar','mime':'image/png','size':len(body)})
  self.assertIn(denied.status_code,(403,404))
