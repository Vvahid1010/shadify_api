"""Ancillary public release proposals; never an audio approval or authority gate."""
import hashlib,json,time,logging
from uuid import UUID,uuid4
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .musicbrainz import text,ProviderUnavailable

def release_request(album,artist):
 album=text(album,200)
 if not album:raise ValueError('Album required')
 artist=str(UUID(artist))
 request=dict(resource='release_suggestions',format_version=1,query=dict(album=album,artist_mbid=artist),limit=5,page=0)
 key=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
 return key,request

def release_query(query):
 if type(query) is not dict or set(query)!={'album','artist_mbid'}:raise ValueError('Release query invalid')
 return release_request(query['album'],query['artist_mbid'])[1]['query']

def release_snapshot(raw,query):
 try:
  query=release_query(query)
  if type(raw) is not bytes or len(raw)>1048576:raise ValueError()
  document=json.loads(raw);rows=document['releases'];total=document['count']
  if type(rows) is not list or type(total) is not int or not 0<=total<=999999999 or len(rows)!=min(total,5):raise ValueError()
  if type(document.get('offset',0)) is not int or document.get('offset',0)!=0:raise ValueError()
  candidates=[]
  for row in rows:
   credits=row['artist-credit']
   if type(credits) is not list or len(credits)!=1 or str(UUID(credits[0]['artist']['id']))!=query['artist_mbid']:raise ValueError()
   credit=text(credits[0].get('name',credits[0]['artist'].get('name','')),120);title=text(row['title'],200)
   if not credit or not title:raise ValueError()
   candidates.append(dict(external_release_id=str(UUID(row['id'])),title=title,artist_credit=credit,artist_mbid=query['artist_mbid']))
  if len({v['external_release_id'] for v in candidates})!=len(candidates):raise ValueError()
  return dict(candidates=candidates,total=total,page_complete=True,search_complete=total<=5,has_more=total>5)
 except (ValueError,KeyError,TypeError,AttributeError,OverflowError):raise ProviderUnavailable('Invalid release response') from None

def available(c):
 # Detection and privilege probing remain inside a savepoint. A missing table or
 # optional SELECT denial never leaves the caller's ordinary transaction aborted.
 try:
  with c.connection.transaction():
   if c.execute("SELECT to_regclass('shadify_ingest_release_proposals') AS name").fetchone()['name'] is None:return False
   c.execute('SELECT id FROM shadify_ingest_release_proposals LIMIT 0')
  return True
 except psycopg.Error:return False

def projection(c,job_id):
 empty=dict(status='not_available',proposal_id=None,candidates=[],has_more=False,reason='feature_unavailable')
 if not available(c):return empty
 row=c.execute('SELECT id,result FROM shadify_ingest_release_proposals WHERE job_id=%s AND format_version=1',(job_id,)).fetchone()
 if row is None:return {**empty,'reason':'unverified_artist_or_missing_album'}
 if row['result'] is None:return {**empty,'status':'pending','proposal_id':str(row['id']),'reason':'pending_provider'}
 result=row['result']
 return dict(status='available',proposal_id=str(row['id']),candidates=[{k:v[k] for k in ('external_release_id','title','artist_credit')} for v in result['candidates']],has_more=result['has_more'],page_complete=True,search_complete=result['search_complete'],provider='musicbrainz',format_version=1)

def complete(c,key,result,digest,fetched_at):
 if not available(c):return
 try:
  with c.connection.transaction():
   c.execute('UPDATE shadify_ingest_release_proposals SET result=%s,response_sha256=%s,provider_fetched_at=%s,recorded_at=clock_timestamp() WHERE request_key=%s AND result IS NULL',(Jsonb(result),digest,fetched_at,key))
 except psycopg.Error:
  logging.getLogger(__name__).warning('Optional release snapshot unavailable')
  return False
 return True

def reconcile(connect,provider,limit=10):
 if not hasattr(provider,'lookup_release'):return
 # Bounded source reconciliation catches decision->proposal crashes; no job or
 # closed stage is updated. Proposals do not wait inside the file attempt lease.
 with connect() as connection,connection.cursor(row_factory=dict_row) as c:
  if not available(c):return
  rows=c.execute("""SELECT j.id AS job_id,j.input_sha256,e.id AS extraction_id,e.payload AS extraction,d.id AS decision_id,d.selected_recording_mbid,d.corrected_metadata,m.payload AS matching
   FROM shadify_ingest_jobs j JOIN shadify_ingest_stages e ON e.job_id=j.id AND e.input_sha256=j.input_sha256 AND e.stage='extraction'
   JOIN shadify_ingest_match_decisions d ON d.job_id=j.id AND d.input_sha256=j.input_sha256
   JOIN shadify_ingest_stages m ON m.id=d.matching_stage_id AND m.job_id=j.id AND m.input_sha256=j.input_sha256 AND m.stage='matching'
   WHERE d.kind='audio' AND e.payload->'complete'='true'::jsonb AND d.selected_recording_mbid IS NOT NULL AND m.payload @> '{"format_version":2,"complete":true,"provider":"musicbrainz","truncated":false}'
   AND length(btrim(COALESCE(d.corrected_metadata->>'album',e.payload->'metadata'->>'album',''))) BETWEEN 1 AND 200
   AND EXISTS(SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(m.payload->'candidates')='array' THEN m.payload->'candidates' ELSE '[]'::jsonb END) item WHERE item->>'mbid'=d.selected_recording_mbid::text AND CASE WHEN jsonb_typeof(item->'artist_credits')='array' THEN jsonb_array_length(item->'artist_credits') ELSE 0 END=1)
   AND NOT EXISTS(SELECT 1 FROM shadify_ingest_release_proposals p WHERE p.job_id=j.id AND p.format_version=1)
   ORDER BY j.id LIMIT %s""",(limit,)).fetchall()
  for row in rows:
   try:
    candidate=next(v for v in row['matching']['candidates'] if v['mbid']==str(row['selected_recording_mbid']))
    artist=candidate['artist_credits'][0]['id'];album=row['corrected_metadata'].get('album',row['extraction']['metadata'].get('album',''))
    key,request=release_request(album,artist);album=request['query']['album'];artist=request['query']['artist_mbid']
   except (ValueError,TypeError,KeyError,StopIteration):continue
   c.execute('INSERT INTO shadify_ingest_release_proposals(id,job_id,input_sha256,extraction_stage_id,recording_decision_id,artist_mbid,album,request_key,request,format_version) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,1) ON CONFLICT(job_id,format_version) DO NOTHING',(uuid4(),row['job_id'],row['input_sha256'],row['extraction_id'],row['decision_id'],artist,album,key,Jsonb(request)))
 # Insert commits before queue admission. A crash here leaves a durable pending
 # binding, which the next pass resumes without resetting queue attempts/pauses.
 with connect() as connection,connection.cursor(row_factory=dict_row) as c:
  pending=c.execute("""WITH picked AS (
   SELECT id FROM shadify_ingest_release_proposals WHERE result IS NULL
   ORDER BY last_reconciled_at NULLS FIRST,created_at,id FOR UPDATE SKIP LOCKED LIMIT %s
  ) UPDATE shadify_ingest_release_proposals p SET last_reconciled_at=clock_timestamp()
   FROM picked WHERE p.id=picked.id RETURNING p.request_key,p.request""",(limit,)).fetchall()
 for row in pending:
  try:result=provider.lookup_release(row['request'],deadline=time.monotonic()+12)
  except ProviderUnavailable:continue
  with connect() as connection,connection.cursor(row_factory=dict_row) as c:
   complete(c,row['request_key'],result['snapshot'],result['snapshot_sha256'],result['fetched_at'])

def safe_reconcile(connect,provider,limit=10):
 # Separate connections/transactions roll back optional failures, never audio.
 try:reconcile(connect,provider,limit)
 except Exception:logging.getLogger(__name__).warning('Optional release reconciliation unavailable')
