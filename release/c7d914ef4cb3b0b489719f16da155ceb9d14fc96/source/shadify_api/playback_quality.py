"""Full local quality from the current track delivery anchor, never latest output."""
from dataclasses import replace
from psycopg.rows import dict_row
from .delivery import require_playable
from .media import Asset,MediaError
from .local_storage import LocalAdapter
from .storage import StorageUnavailable
import os

def source(repository,track_id):
 target=repository.get_playback_target(track_id,preview=False)
 anchor=require_playable(target,track_id,preview=False)
 if anchor.storage_provider!='local':raise MediaError(404,'Local playback unavailable')
 repository.require_ingest_approval(anchor)
 with repository.connect() as connection,connection.cursor(row_factory=dict_row) as c:
  row=c.execute("SELECT v.job_id FROM shadify_ingest_variants v JOIN shadify_ingest_jobs j ON j.id=v.job_id JOIN shadify_media_assets input ON input.id=j.asset_id WHERE v.asset_id=%s AND v.role IN ('high','low') AND j.state='ready' AND input.artist_id=%s AND input.track_id=%s",(anchor.id,anchor.artist_id,track_id)).fetchone()
 if row is None:raise MediaError(409,'Requested audio quality unavailable')
 return target,anchor,row['job_id']

def variant(repository,target,job_id,role):
 with repository.connect() as connection,connection.cursor(row_factory=dict_row) as c:
  row=c.execute('SELECT a.* FROM shadify_ingest_variants v JOIN shadify_media_assets a ON a.id=v.asset_id WHERE v.job_id=%s AND v.role=%s AND a.artist_id=%s AND a.track_id=%s',(job_id,role,target.asset.artist_id,target.track_id)).fetchone()
 if row is None:raise MediaError(409,'Requested audio quality unavailable')
 asset=Asset(**row)
 require_playable(replace(target,asset=asset),target.track_id,preview=False)
 if asset.storage_provider!='local':raise MediaError(409,'Requested audio quality unavailable')
 repository.require_ingest_approval(asset)
 return asset

def select(storage,track_id,quality,save_data=False):
 if quality not in ('auto','high','data_saver') or type(save_data) is not bool:raise MediaError(422,'Invalid playback quality')
 target,anchor,job=source(storage.repository,track_id)
 preferred='low' if quality=='data_saver' or quality=='auto' and save_data else 'high'
 roles=[preferred, 'high' if preferred=='low' else 'low'] if quality=='auto' else [preferred]
 for role in roles:
  try:
   asset=variant(storage.repository,target,job,role);adapter=storage.adapter(asset)
   if not isinstance(adapter,LocalAdapter):raise StorageUnavailable('Local playback required')
   fd=adapter.open(asset.key)
   try:storage.require_file(asset,fd)
   finally:os.close(fd)
  except StorageUnavailable:continue
  except MediaError as exc:
   if exc.status not in (404,409):raise
   continue
  return dict(asset=asset,delivery_anchor=anchor.id,source_job=job,variant_role=role,selected_quality='data_saver' if role=='low' else 'high',fallback=role!=preferred)
 raise MediaError(409,'Requested audio quality unavailable')

def revalidate(repository,claims,asset,anchor):
 # One READ COMMITTED statement AFTER every approval call: rights, current
 # anchor, ready job, role, selected asset and approval descriptors are coherent.
 # No further approval callback may wait after this final authoritative sample.
 with repository.connect() as connection,connection.cursor(row_factory=dict_row) as c:
  row=c.execute("""SELECT a.*,t.publish_state AS track_state,t.access_class AS track_access,
   page.publish_state AS page_state,anchor.id AS anchor_id,av.job_id AS source_job,
   anchor.state AS anchor_state,anchor.publish_state AS anchor_publish,
   anchor.access_class AS anchor_access,anchor.key AS anchor_key,
   anchor.asset_type AS anchor_type,anchor.storage_provider AS anchor_provider
   FROM shadify_tracks t JOIN shadify_artists page ON page.id=t.artist_id
   JOIN shadify_media_assets anchor ON anchor.id=t.delivery_asset_id AND anchor.artist_id=t.artist_id AND anchor.track_id=t.id
   JOIN shadify_ingest_variants av ON av.asset_id=anchor.id AND av.role IN ('high','low')
   JOIN shadify_ingest_jobs j ON j.id=av.job_id AND j.state='ready'
   JOIN shadify_media_assets input ON input.id=j.asset_id AND input.artist_id=t.artist_id AND input.track_id=t.id
   JOIN shadify_ingest_variants v ON v.job_id=j.id AND v.role=%s
   JOIN shadify_media_assets a ON a.id=v.asset_id AND a.artist_id=t.artist_id AND a.track_id=t.id
   JOIN shadify_ingest_approvals approved ON approved.asset_id=a.id AND approved.job_id=j.id
    AND approved.input_sha256=j.input_sha256 AND approved.storage_key=a.key
    AND approved.storage_provider=a.storage_provider AND approved.mime=a.mime AND approved.size=a.size
   JOIN shadify_ingest_approvals anchor_approved ON anchor_approved.asset_id=anchor.id AND anchor_approved.job_id=j.id
    AND anchor_approved.input_sha256=j.input_sha256 AND anchor_approved.storage_key=anchor.key
    AND anchor_approved.storage_provider=anchor.storage_provider AND anchor_approved.mime=anchor.mime AND anchor_approved.size=anchor.size
   WHERE t.id=%s""",(claims['variant_role'],asset.track_id)).fetchone()
 if row is None:raise MediaError(404,'Playback source changed')
 fields={k:row.pop(k) for k in ('track_state','track_access','page_state','anchor_id','source_job','anchor_state','anchor_publish','anchor_access','anchor_key','anchor_type','anchor_provider')}
 if str(fields['anchor_id'])!=claims['delivery_anchor'] or str(fields['source_job'])!=claims['source_job'] or Asset(**row)!=asset:raise MediaError(404,'Playback source changed')
 from .delivery import PlaybackTarget
 target=PlaybackTarget(asset.track_id,fields['track_state'],fields['track_access'],asset,fields['page_state'])
 require_playable(target,asset.track_id,preview=False)
 current_anchor=replace(anchor,state=fields['anchor_state'],publish_state=fields['anchor_publish'],access_class=fields['anchor_access'],key=fields['anchor_key'],asset_type=fields['anchor_type'],storage_provider=fields['anchor_provider'])
 require_playable(replace(target,asset=current_anchor),asset.track_id,preview=False)

def authorize(repository,claims,asset):
 target,anchor,job=source(repository,asset.track_id)
 if str(anchor.id)!=claims['delivery_anchor'] or str(job)!=claims['source_job']:raise MediaError(404,'Playback source changed')
 selected=variant(repository,target,job,claims['variant_role'])
 if selected!=asset:raise MediaError(404,'Playback source changed')
 revalidate(repository,claims,asset,anchor)
