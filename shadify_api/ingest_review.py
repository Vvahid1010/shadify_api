"""Decisions only; state/revision/approval writes belong to the worker."""
from uuid import UUID,uuid4
from pydantic import BaseModel,ConfigDict,Field
from psycopg.types.json import Jsonb
from .media import MediaError
from .auth import require_fresh_actor
from .musicbrainz import text

class MatchReview(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    expected_revision:int=Field(ge=1)
    selected_recording_mbid:str|None=None
    reason:str=Field(min_length=1,max_length=1000)
    corrected_metadata:dict=Field(default_factory=dict)

def corrections(values):
    if type(values) is not dict or not values.keys()<= {'title','artist','album','track_number','isrc'}:raise MediaError(422,'Invalid corrected metadata')
    try:return {key:text(value,200 if key in ('title','album') else 120 if key=='artist' else 32) for key,value in values.items()}
    except ValueError:raise MediaError(422,'Invalid corrected metadata') from None

class IngestReviewRepository:
    def match_review(self,actor,asset_id,body=None):
        asset=self.get(asset_id)
        if asset is None:raise MediaError(404,'Asset not found')
        operation='read_content' if body is None else 'manage_content'
        with self.authorized(actor,asset.artist_id,operation=operation) as (c,auth,_,_):
            if c.execute("SELECT to_regclass('shadify_ingest_stages') AS name").fetchone()['name'] is None:raise MediaError(503,'Matching integration pending')
            if c.execute('SELECT 1 FROM shadify_tracks WHERE id=%s AND artist_id=%s FOR SHARE',(asset.track_id,asset.artist_id)).fetchone() is None:raise MediaError(404,'Track not found')
            job=c.execute('SELECT * FROM shadify_ingest_jobs WHERE asset_id=%s FOR UPDATE',(asset_id,)).fetchone()
            if job is None:raise MediaError(409,'Ingest unavailable')
            stage=c.execute("SELECT * FROM shadify_ingest_stages WHERE job_id=%s AND stage='matching'",(job['id'],)).fetchone()
            decision=c.execute('SELECT * FROM shadify_ingest_match_decisions WHERE job_id=%s',(job['id'],)).fetchone()
            extraction=c.execute("SELECT payload FROM shadify_ingest_stages WHERE job_id=%s AND stage='extraction'",(job['id'],)).fetchone()
            if body is not None:
                if job['state']!='review_required' or job['revision']!=body.expected_revision or stage is None or decision is not None:raise MediaError(409,'Matching decision or revision changed')
                if asset.asset_type!='audio_original' or stage['payload'].get('complete') is not True:raise MediaError(409,'Completed audio lookup required')
                selected=body.selected_recording_mbid
                if selected is not None:
                    try:selected=str(UUID(selected))
                    except ValueError:raise MediaError(422,'Invalid candidate') from None
                    if selected not in {v['mbid'] for v in stage['payload']['candidates']}:raise MediaError(422,'Candidate not in server lookup')
                metadata=corrections(body.corrected_metadata)
                try:reason=text(body.reason,1000)
                except ValueError:raise MediaError(422,'Invalid reason') from None
                if not reason:raise MediaError(422,'Review reason required')
                decision={'id':uuid4(),'method':'reviewed','selected_recording_mbid':selected,'corrected_metadata':metadata,'reason':reason}
                c.execute("INSERT INTO shadify_ingest_match_decisions(id,job_id,input_sha256,matching_stage_id,kind,method,selected_recording_mbid,corrected_metadata,reason,actor_user_id) VALUES (%s,%s,%s,%s,'audio','reviewed',%s,%s,%s,%s)",
                    (decision['id'],job['id'],job['input_sha256'],stage['id'],selected,Jsonb(metadata),reason,auth.user_id))
                require_fresh_actor(actor)
                require_fresh_actor(actor)
            return dict(job_id=str(job['id']),revision=job['revision'],state=job['state'],
                extracted_metadata=extraction['payload'].get('metadata',{}) if extraction else {},
                lookup_complete=stage is not None,candidates=stage['payload'].get('candidates',[]) if stage else [],
                decision=None if decision is None else dict(id=str(decision['id']),method=decision['method'],selected_recording_mbid=str(decision['selected_recording_mbid']) if decision['selected_recording_mbid'] else None,corrected_metadata=decision['corrected_metadata']))
