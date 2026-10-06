"""New Studio read projection: isolated PG + synthetic existing pipeline only."""
import json,time,unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID,uuid4
from unittest.mock import patch
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from authority_fixture import admitted_actor
from shadify_api.main import create_app
from shadify_api.domain import DomainService
from shadify_api.studio_uploads import matching_projection
from test_ingest_pipeline import PipelineTests,MBID,safe_png
from test_ingest_foundation import FixtureScanner

class StudioUploadTests(PipelineTests):
 def page(self,**params):return self.api.get('/api/artists/'+str(self.artist)+'/uploads',params=params)
 def items(self,**params):
  response=self.page(**params);self.assertEqual(response.status_code,200,response.text)
  return response.json()['items']
 def test_pending_failed_uploaded_and_legacy_survive_refresh_without_usable_file(self):
  _,pending=self.pending();asset,_,_=self.upload()
  _,failed=self.pending()
  self.assertTrue(self.repo.transition(admitted_actor('owner-a'),UUID(failed['asset_id']),'upload_pending','failed'))
  rows={v['asset_id']:v for v in self.items()}
  self.assertEqual(rows[pending['asset_id']]['state'],'upload_pending');self.assertIsNone(rows[pending['asset_id']]['ingest'])
  self.assertEqual(rows[failed['asset_id']]['state'],'failed');self.assertIsNone(rows[failed['asset_id']]['matching'])
  self.assertEqual(rows[str(asset.id)]['ingest']['state'],'uploaded');self.assertFalse(rows[str(asset.id)]['matching']['lookup_complete'])
  self.assertTrue(rows[str(self.legacy.id)]['ingest']['legacy_unregistered']);self.assertFalse(rows[str(self.legacy.id)]['ingest']['approved'])
  for id_,status in ((pending['asset_id'],404),(str(asset.id),409),(str(self.legacy.id),409)):
   self.assertEqual(self.api.post('/api/media/'+id_+'/access').status_code,status)
 def test_ready_original_excludes_all_produced_masters_and_variants(self):
  asset=self.audio_ready();rows=self.items()
  row=next(v for v in rows if v['asset_id']==str(asset.id))
  self.assertEqual(row['ingest']['state'],'ready');self.assertFalse(row['ingest']['approved'])
  self.assertTrue(row['matching']['lookup_complete']);self.assertEqual(row['matching']['decision']['method'],'automatic')
  self.assertEqual(row['matching']['candidates'][0]['mbid'],MBID)
  produced={v['asset_id'] for v in self.outputs(asset)}
  self.assertTrue(produced.isdisjoint({v['asset_id'] for v in rows}))
  self.assertEqual(self.api.post('/api/media/'+str(asset.id)+'/access').status_code,409)
 def test_artwork_has_independent_job_without_audio_lookup_or_duplicate_outputs(self):
  self.audio_ready()
  with patch('test_ingest_foundation.PNG',safe_png()):cover,_,_=self.upload(cover=True)
  self.assertEqual([self.once()['state'] for _ in range(4)],['validating','matching','processing','ready'])
  row=next(v for v in self.items() if v['asset_id']==str(cover.id))
  self.assertEqual(row['ingest']['state'],'ready');self.assertFalse(row['ingest']['approved'])
  self.assertFalse(row['matching']['lookup_complete']);self.assertEqual(row['matching']['candidates'],[])
  self.assertEqual(row['matching']['decision']['method'],'automatic');self.assertIsNone(row['matching']['decision']['selected_recording_mbid'])
  produced={v['asset_id'] for v in self.outputs(cover)}
  self.assertTrue(produced.isdisjoint({v['asset_id'] for v in self.items()}))

 def test_ambiguous_and_provider_retry_states_read_without_provider_dispatch(self):
  asset,_,_=self.upload();self.provider.ambiguous=True
  self.once();self.once();self.assertEqual(self.once()['state'],'review_required')
  calls=self.provider.calls
  with patch.object(self.provider,'lookup',side_effect=AssertionError('No provider on read')):
   row=next(v for v in self.items() if v['asset_id']==str(asset.id))
  self.assertEqual(self.provider.calls,calls);self.assertTrue(row['matching']['lookup_complete'])
  self.assertEqual(row['matching']['candidates'],[]);self.assertIsNone(row['matching']['decision'])
  self.assertFalse(row['ingest']['approved']);self.assertEqual(row['ingest']['state'],'review_required')
  other,_,_=self.upload();self.provider.failures=1
  self.once();self.once();self.assertEqual(self.once()['last_error'],'provider_unavailable')
  row=next(v for v in self.items() if v['asset_id']==str(other.id))
  self.assertTrue(row['ingest']['retryable']);self.assertFalse(row['matching']['lookup_complete'])
 def test_historical_partial_matching_stage_never_repeats_automatic_choice(self):
  for total in (2,5):
   asset,_,_=self.upload();job=self.job(asset)
   self.scanner(FixtureScanner()).scan_job(job['job_id'],expected_revision=1)
   stage=uuid4();decision=uuid4()
   evidence=dict(complete=True,provider='musicbrainz',truncated=False,total=total,automatic_mbid=MBID,candidates=[dict(mbid=MBID,title='Quarantine proof',artist='Test',duration_ms=2,isrc='USAAA2600001')])
   with self.connect() as c:
    c.execute("INSERT INTO shadify_ingest_stages(id,job_id,input_sha256,scan_id,stage,payload) SELECT %s,job_id,input_sha256,id,'matching',%s FROM shadify_ingest_scans WHERE job_id=%s AND verdict='clean'",(stage,Jsonb(evidence),UUID(job['job_id'])))
    c.execute("INSERT INTO shadify_ingest_match_decisions(id,job_id,input_sha256,matching_stage_id,kind,method,selected_recording_mbid,corrected_metadata,reason) SELECT %s,id,input_sha256,%s,'audio','automatic',%s,'{}','Historical isolated partial snapshot' FROM shadify_ingest_jobs WHERE id=%s",(decision,stage,UUID(MBID),UUID(job['job_id'])))
   row=next(v for v in self.items() if v['asset_id']==str(asset.id))
   self.assertFalse(row['matching']['lookup_complete']);self.assertEqual(row['matching']['candidates'],[]);self.assertIsNone(row['matching']['decision'])
   self.assertFalse(row['ingest']['approved']);self.assertEqual(self.api.post('/api/media/'+str(asset.id)+'/access').status_code,409)
 def test_batch_pagination_track_filter_and_foreign_cursor_preserve_page_boundary(self):
  second=UUID(self.api.post('/api/artists/'+str(self.artist)+'/tracks',json={'title':'Second batch track'}).json()['track_id'])
  ids={str(self.legacy.id)}
  for track in (self.track,second,self.track,second):
   self.track=track;_,dto=self.pending();ids.add(dto['asset_id'])
  seen=[];cursor=None
  while True:
   response=self.page(limit=2,**({'cursor':cursor} if cursor else {}));self.assertEqual(response.status_code,200,response.text)
   dto=response.json();seen.extend(v['asset_id'] for v in dto['items']);cursor=dto['next_cursor']
   if cursor is None:break
  self.assertEqual(seen,sorted(ids));self.assertEqual(len(seen),len(set(seen)))
  self.assertTrue(all(v['track_id']==str(second) for v in self.items(track_id=str(second))))
  self.assertEqual(self.page(track_id=str(uuid4())).status_code,404)
  self.assertEqual(self.page(limit=0).status_code,422);self.assertEqual(self.page(limit=21).status_code,422)
  self.assertEqual(self.page(cursor='invalid').status_code,422)
  self.assertEqual(self.page(cursor=str(uuid4())).status_code,200)
  self.assign(self.other,'owner-b');self.actor='owner-b'
  foreign=UUID(self.api.post('/api/artists/'+str(self.other)+'/tracks',json={'title':'Other page'}).json()['track_id'])
  self.actor='owner-a';self.assertEqual(self.page(track_id=str(foreign)).status_code,404)
 def test_owner_delegate_primary_and_uploader_are_separate(self):
  self.repo.set_delegate(admitted_actor('admin'),'listener',['manage_content'],expected_revision=self.admin_revision())
  self.actor='listener';_,dto=self.pending()
  self.actor='owner-a';self.assertIn(dto['asset_id'],{v['asset_id'] for v in self.items()})
  self.actor='admin';self.assertEqual(self.page().status_code,200)
  self.actor='owner-b';self.assertEqual(self.page().status_code,404)
  self.repo.set_delegate(admitted_actor('admin'),'listener',['verify'],expected_revision=self.admin_revision())
  self.actor='listener';self.assertEqual(self.page().status_code,404)
  self.repo.set_delegate(admitted_actor('admin'),'listener',[],expected_revision=self.admin_revision())
  self.assertEqual(self.page().status_code,404)
  self.actor='owner-a';self.assertEqual(self.api.get('/api/artists/'+str(self.artist)).status_code,200)
 def test_expiry_after_projection_and_revocation_before_authority_prevent_response(self):
  now=int(time.time());actor=admitted_actor('owner-a',issued=now,expires=now+60);clock=[now]
  api=TestClient(create_app(self.service,lambda:actor,repository=self.repo,domain=DomainService(self.repo)))
  _,dto=self.pending();asset=UUID(dto['asset_id'])
  # Projection helper runs after authorized SQL read; final existing identity
  # fence must reject instead of letting a completed DTO out after expiry.
  from shadify_api import studio_uploads
  original=studio_uploads.matching_projection
  self.repo.transition(admitted_actor('owner-a'),asset,'upload_pending','uploaded',byte_sha256='0'*64)
  def expire(*args,**kwargs):
   result=original(*args,**kwargs);clock[0]=actor.expires_at;return result
  with patch('shadify_api.auth.time.time',side_effect=lambda:clock[0]),patch('shadify_api.studio_uploads.matching_projection',side_effect=expire):
   self.assertEqual(api.get('/api/artists/'+str(self.artist)+'/uploads').status_code,401)
  self.repo.set_delegate(admitted_actor('admin'),'listener',['manage_content'],expected_revision=self.admin_revision())
  self.repo.set_delegate(admitted_actor('admin'),'listener',[],expected_revision=self.admin_revision())
  self.actor='listener';self.assertEqual(self.page().status_code,404)
 def test_waiting_read_loses_to_committing_delegate_revocation(self):
  self.repo.set_delegate(admitted_actor('admin'),'listener',['manage_content'],expected_revision=self.admin_revision())
  revision=self.admin_revision();entered=Event();release=Event();reader_started=Event()
  original=self.repo._admin_audit
  def blocked_audit(*args,**kwargs):
   result=original(*args,**kwargs);entered.set();self.assertTrue(release.wait(5));return result
  def identity():reader_started.set();return admitted_actor('listener')
  api=TestClient(create_app(self.service,identity,repository=self.repo,domain=DomainService(self.repo)))
  with patch.object(self.repo,'_admin_audit',side_effect=blocked_audit),ThreadPoolExecutor(2) as pool:
   writer=pool.submit(self.repo.set_delegate,admitted_actor('admin'),'listener',[],expected_revision=revision)
   self.assertTrue(entered.wait(3))
   reader=pool.submit(api.get,'/api/artists/'+str(self.artist)+'/uploads')
   self.assertTrue(reader_started.wait(3));self.assertFalse(reader.done())
   release.set();writer.result(3);self.assertEqual(reader.result(3).status_code,404)

 def test_public_and_error_paths_leak_no_storage_or_worker_evidence(self):
  asset,_,_=self.upload();body=self.page().text
  self.assertNotIn(asset.key,body);self.assertNotIn('snapshot_sha256',body);self.assertNotIn('storage_provider',body);self.assertNotIn('url',body)
  self.assertNotIn('owner_id',body);self.assertNotIn('input_sha256',body);self.assertNotIn('reason',body)
  with self.connect() as c:c.execute('DROP TABLE shadify_ingest_stages CASCADE')
  self.assertEqual(self.page().status_code,503)
  self.assertEqual(self.api.get('/api/me').status_code,200)
  self.assertEqual(self.api.get('/api/artists/'+str(self.artist)).status_code,200)

class ProjectionTests(unittest.TestCase):
 def test_malformed_or_truncated_saved_lookup_cannot_be_complete(self):
  candidate=dict(mbid=MBID,title='Song',artist='Artist',duration_ms=1,isrc='USAAA2600001')
  evidence=dict(complete=True,provider='musicbrainz',total=1,truncated=False,candidates=[candidate])
  row=dict(asset_type='audio_original',decision_id=None)
  self.assertTrue(matching_projection(None,evidence,row)['lookup_complete'])
  for changed in ({'complete':False},{'total':True},{'total':2},{'truncated':True},{'candidates':[candidate,candidate]},{'candidates':[{**candidate,'duration_ms':True}]},{'candidates':[{**candidate,'title':'bad\nvalue'}]}):
   result=matching_projection(None,{**evidence,**changed},row)
   self.assertFalse(result['lookup_complete']);self.assertEqual(result['candidates'],[])

def load_tests(loader,tests,pattern):
 suite=loader.loadTestsFromTestCase(ProjectionTests)
 suite.addTests(StudioUploadTests(name) for name in StudioUploadTests.__dict__ if name.startswith('test_'))
 return suite
