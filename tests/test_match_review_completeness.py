"""Saved-evidence review guard; isolated PostgreSQL/HTTP, no provider traffic."""
import unittest
from uuid import UUID,uuid4
from unittest.mock import patch
from psycopg.types.json import Jsonb
from test_ingest_pipeline import PipelineTests,MBID
from test_ingest_foundation import FixtureScanner

class MatchReviewCompletenessTests(PipelineTests):
 def evidence(self,**changes):
  candidate=dict(mbid=MBID,title='Quarantine proof',artist='Test',duration_ms=2,isrc='USAAA2600001')
  return {**dict(complete=True,provider='musicbrainz',total=1,truncated=False,candidates=[candidate],automatic_mbid=None),**changes}
 def saved_review(self,evidence):
  asset,_,_=self.upload();self.once();self.once()
  # Simulate already-saved historical evidence, not today's validated provider.
  with patch.object(self.provider,'lookup',return_value=evidence):
   self.assertEqual(self.once()['state'],'review_required')
  return asset,'/api/media/'+str(asset.id)+'/match-review',dict(expected_revision=self.job(asset)['revision'],reason='Explicit isolated review',selected_recording_mbid=None)
 def decision_count(self,asset):
  with self.connect() as c:return c.execute('SELECT count(*) FROM shadify_ingest_match_decisions d JOIN shadify_ingest_jobs j ON j.id=d.job_id WHERE j.asset_id=%s',(asset.id,)).fetchone()[0]
 def test_partial_incomplete_truncated_and_invalid_candidate_evidence_cannot_be_reviewed(self):
  candidate=self.evidence()['candidates'][0]
  cases=[{'complete':False},{'total':2},{'total':5},{'total':True},{'total':None},{'truncated':True},{'truncated':None},
         {'provider':'other'},{'candidates':[candidate,candidate],'total':2},
         {'candidates':[{**candidate,'mbid':'invalid'}]},
         {'candidates':[{**candidate,'duration_ms':True}]},
         {'candidates':[{**candidate,'title':'bad\nvalue'}]},
         {'candidates':[candidate]*6,'total':6}]
  for changed in cases:
   with self.subTest(changed=changed):
    asset,path,body=self.saved_review(self.evidence(**changed))
    response=self.api.get(path);self.assertEqual(response.status_code,200,response.text)
    view=response.json();self.assertFalse(view['lookup_complete']);self.assertEqual(view['candidates'],[]);self.assertIsNone(view['decision'])
    for selected in (None,MBID):
     self.assertEqual(self.api.put(path,json={**body,'selected_recording_mbid':selected}).status_code,409)
    self.assertEqual(self.decision_count(asset),0);self.assertEqual(self.job(asset)['revision'],body['expected_revision'])
    self.assertFalse(self.job(asset)['approved']);self.assertEqual(self.api.post('/api/media/'+str(asset.id)+'/access').status_code,409)
 def test_historical_partial_automatic_decision_is_not_repeated_by_read(self):
  for total in (2,5):
   asset,_,_=self.upload();job=self.job(asset)
   self.scanner(FixtureScanner()).scan_job(job['job_id'],expected_revision=job['revision'])
   path='/api/media/'+str(asset.id)+'/match-review';body=dict(expected_revision=self.job(asset)['revision'],reason='Historical partial review')
   with self.connect() as c:
    c.execute("INSERT INTO shadify_ingest_stages(id,job_id,input_sha256,scan_id,stage,payload) SELECT %s,job_id,input_sha256,id,'matching',%s FROM shadify_ingest_scans WHERE job_id=%s AND verdict='clean'",(uuid4(),Jsonb(self.evidence(total=total,automatic_mbid=MBID)),UUID(job['job_id'])))
    c.execute("INSERT INTO shadify_ingest_match_decisions(id,job_id,input_sha256,matching_stage_id,kind,method,selected_recording_mbid,corrected_metadata,reason) SELECT %s,j.id,j.input_sha256,s.id,'audio','automatic',%s,'{}','Isolated historical partial decision' FROM shadify_ingest_jobs j JOIN shadify_ingest_stages s ON s.job_id=j.id AND s.stage='matching' WHERE j.asset_id=%s",(uuid4(),UUID(MBID),asset.id))
   view=self.api.get(path).json();self.assertFalse(view['lookup_complete']);self.assertEqual(view['candidates'],[]);self.assertIsNone(view['decision'])
   self.assertEqual(self.api.put(path,json=body).status_code,409);self.assertEqual(self.decision_count(asset),1)
 def test_complete_saved_candidate_remains_bounded_and_reviewable(self):
  evidence=self.evidence();evidence['candidates'][0]['private_url']='https://private.invalid/token'
  evidence['raw_provider']={'secret':'PRIVATE-EVIDENCE'}
  asset,path,body=self.saved_review(evidence)
  before=self.api.get(path);view=before.json();self.assertTrue(view['lookup_complete'])
  self.assertEqual(set(view['candidates'][0]),{'mbid','title','artist','duration_ms','isrc'})
  self.assertNotIn('private',before.text);self.assertNotIn('PRIVATE-EVIDENCE',before.text)
  self.assertEqual(self.api.put(path,json={**body,'selected_recording_mbid':str(uuid4())}).status_code,422)
  response=self.api.put(path,json={**body,'selected_recording_mbid':MBID,'corrected_metadata':{'title':'Reviewed title'}})
  self.assertEqual(response.status_code,200,response.text);after=response.json()
  self.assertEqual(after['revision'],body['expected_revision']);self.assertEqual(after['state'],'review_required')
  self.assertEqual(after['decision']['method'],'reviewed');self.assertEqual(after['decision']['selected_recording_mbid'],MBID)
  self.assertEqual(after['decision']['corrected_metadata'],{'title':'Reviewed title'})
  self.assertEqual(self.api.get(path).json(),after);self.assertEqual(self.api.put(path,json=body).status_code,409)
  self.assertEqual(self.decision_count(asset),1);self.assertFalse(self.job(asset)['approved'])
  self.assertEqual(self.once()['state'],'ready')
 def test_complete_empty_lookup_allows_explicit_no_match(self):
  asset,path,body=self.saved_review(self.evidence(total=0,candidates=[]))
  view=self.api.get(path).json();self.assertTrue(view['lookup_complete']);self.assertEqual(view['candidates'],[])
  response=self.api.put(path,json=body);self.assertEqual(response.status_code,200,response.text)
  self.assertIsNone(response.json()['decision']['selected_recording_mbid']);self.assertEqual(self.decision_count(asset),1)
  self.assertEqual(self.once()['state'],'ready')
 def test_missing_stage_is_incomplete_and_no_lookup_is_dispatched_on_read(self):
  asset,_,_=self.upload();path='/api/media/'+str(asset.id)+'/match-review'
  with patch.object(self.provider,'lookup',side_effect=AssertionError('Read cannot dispatch')):
   view=self.api.get(path).json();self.assertFalse(view['lookup_complete']);self.assertEqual(view['candidates'],[])
  self.assertEqual(self.api.put(path,json={'expected_revision':view['revision'],'reason':'Not yet matched'}).status_code,409)
  self.assertEqual(self.decision_count(asset),0)
 def test_bounded_extraction_and_corrected_metadata_drop_private_fields(self):
  original=self.tools.validate
  def extraction(*args,**kwargs):
   validation,result=original(*args,**kwargs)
   result['metadata']['private_path']='PRIVATE-PATH';result['metadata']['url']='PRIVATE-URL'
   return validation,result
  with patch.object(self.tools,'validate',side_effect=extraction):asset,path,body=self.saved_review(self.evidence())
  response=self.api.get(path);self.assertEqual(response.status_code,200,response.text)
  self.assertNotIn('PRIVATE',response.text);self.assertEqual(response.json()['extracted_metadata']['title'],'Quarantine proof')
  # Historical immutable decisions may have extra fields; expose musical fields only.
  with self.connect() as c:
   actor=c.execute("SELECT id FROM shadify_users WHERE auth_account_id='owner-a'").fetchone()[0]
   c.execute("INSERT INTO shadify_ingest_match_decisions(id,job_id,input_sha256,matching_stage_id,kind,method,corrected_metadata,reason,actor_user_id) SELECT %s,j.id,j.input_sha256,s.id,'audio','reviewed',%s,'Isolated historical bounded metadata',%s FROM shadify_ingest_jobs j JOIN shadify_ingest_stages s ON s.job_id=j.id AND s.stage='matching' WHERE j.asset_id=%s",(uuid4(),Jsonb({'title':'Saved correction','private_url':'PRIVATE-URL'}),actor,asset.id))
  response=self.api.get(path);self.assertEqual(response.status_code,200,response.text)
  self.assertEqual(response.json()['decision']['corrected_metadata'],{'title':'Saved correction'});self.assertNotIn('PRIVATE',response.text)

def load_tests(loader,tests,pattern):
 return unittest.TestSuite(MatchReviewCompletenessTests(name) for name in MatchReviewCompletenessTests.__dict__ if name.startswith('test_'))
