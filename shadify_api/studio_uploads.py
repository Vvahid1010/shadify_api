"""Read-only Artist Studio queue over existing original intents and pipeline jobs."""
from uuid import UUID
from .ingest import INFRA_ERRORS
from .media import MediaError
from .musicbrainz import text

def musical_metadata(values):
    if type(values) is not dict:raise ValueError('Metadata unavailable')
    result={key:text(values[key],bound) for key,bound in
            (('title',200),('artist',120),('album',200),('track_number',120),('isrc',120)) if key in values}
    if 'duration_ms' in values:
        duration=values['duration_ms']
        if type(duration) is not int or not 1<=duration<=3600000:raise ValueError('Duration unavailable')
        result['duration_ms']=duration
    return result

def matching_projection(extraction,evidence,row):
    metadata=musical_metadata(extraction.get('metadata',{})) if extraction and extraction.get('complete') is True else {}
    candidates=[];complete=False
    if row['asset_type'] in ('audio_original','video_original') and evidence and evidence.get('complete') is True and evidence.get('provider')=='musicbrainz':
        try:
            saved=evidence.get('candidates');total=evidence.get('total')
            if type(saved) is not list or len(saved)>5 or type(total) is not int or total!=len(saved) or evidence.get('truncated') is not False:raise ValueError()
            for candidate in saved:
                if type(candidate) is not dict or type(candidate.get('mbid')) is not str:raise ValueError()
                duration=candidate.get('duration_ms')
                if duration is not None and (type(duration) is not int or not 1<=duration<=3600000):raise ValueError()
                candidates.append(dict(mbid=str(UUID(candidate['mbid'])),title=text(candidate['title'],200),artist=text(candidate['artist'],120),duration_ms=duration,isrc=text(candidate['isrc'],17)))
            if len({v['mbid'] for v in candidates})!=len(candidates):raise ValueError()
            complete=True
        except (ValueError,TypeError,KeyError,AttributeError):candidates=[]
    cover_complete=(row['asset_type']=='artwork_original' and evidence and evidence.get('complete') is True and evidence.get('provider')=='track_audio')
    decision=None
    if row['decision_id'] is not None and (complete or cover_complete):
        decision=dict(id=str(row['decision_id']),method=row['decision_method'],selected_recording_mbid=str(row['selected_recording_mbid']) if row['selected_recording_mbid'] else None,corrected_metadata=musical_metadata(row['corrected_metadata']))
    return dict(extracted_metadata=metadata,lookup_complete=complete,candidates=candidates,decision=decision)

class StudioUploadsRepository:
    def studio_uploads(self,actor,artist_id,*,track_id=None,limit=20,cursor=None):
        if type(limit) is not int or not 1<=limit<=20:raise MediaError(422,'Upload page limit invalid')
        with self.authorized(actor,artist_id,operation='read_content') as (c,auth,_,_):
            if track_id is not None and c.execute('SELECT 1 FROM shadify_tracks WHERE id=%s AND artist_id=%s',(track_id,artist_id)).fetchone() is None:
                raise MediaError(404,'Track not found')
            # One bounded statement snapshots intents/jobs/stages/decisions together.
            # Missing011 schema fails closed through the existing503 boundary.
            rows=c.execute("""SELECT a.id,a.track_id,a.asset_type,a.mime,a.size,a.state,
                j.id AS job_id,j.state AS job_state,j.revision,j.last_error,
                EXISTS(SELECT 1 FROM shadify_ingest_approvals approved WHERE approved.asset_id=a.id AND approved.job_id=j.id) AS approved,
                extraction.payload AS extraction,matching.payload AS matching,
                d.id AS decision_id,d.method AS decision_method,d.selected_recording_mbid,d.corrected_metadata
                FROM shadify_media_assets a
                JOIN shadify_tracks t ON t.id=a.track_id AND t.artist_id=a.artist_id
                LEFT JOIN shadify_ingest_jobs j ON j.asset_id=a.id
                LEFT JOIN shadify_ingest_stages extraction ON extraction.job_id=j.id AND extraction.input_sha256=j.input_sha256 AND extraction.stage='extraction'
                LEFT JOIN shadify_ingest_stages matching ON matching.job_id=j.id AND matching.input_sha256=j.input_sha256 AND matching.stage='matching'
                LEFT JOIN shadify_ingest_match_decisions d ON d.job_id=j.id AND d.input_sha256=j.input_sha256 AND d.matching_stage_id=matching.id
                WHERE a.artist_id=%s AND a.asset_type IN ('audio_original','artwork_original','video_original')
                AND (%s::uuid IS NULL OR a.track_id=%s) AND (%s::uuid IS NULL OR a.id>%s)
                AND NOT EXISTS(SELECT 1 FROM shadify_ingest_variants v WHERE v.asset_id=a.id)
                ORDER BY a.id LIMIT %s""",(artist_id,track_id,track_id,cursor,cursor,limit+1)).fetchall()
            items=[]
            for row in rows[:limit]:
                ingest=None;matching=None
                if row['job_id'] is not None:
                    ingest=dict(job_id=str(row['job_id']),state=row['job_state'],revision=row['revision'],approved=row['job_state']=='ready' and row['approved'],retryable=row['last_error'] in INFRA_ERRORS,last_error=row['last_error'],legacy_unregistered=False)
                    try:matching=matching_projection(row['extraction'],row['matching'],row)
                    except (ValueError,TypeError,KeyError,AttributeError):raise MediaError(503,'Upload metadata unavailable') from None
                elif row['state'] in ('uploaded','ready'):
                    ingest=dict(job_id=None,state='uploaded',revision=None,approved=False,retryable=False,last_error=None,legacy_unregistered=True)
                items.append(dict(asset_id=str(row['id']),track_id=str(row['track_id']),asset_type=row['asset_type'],mime=row['mime'],size=row['size'],state=row['state'],ingest=ingest,matching=matching))
            return {**auth.dto(),'items':items,'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None}
