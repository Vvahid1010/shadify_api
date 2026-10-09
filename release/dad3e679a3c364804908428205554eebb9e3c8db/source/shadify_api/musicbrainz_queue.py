"""App-owned bounded public response cache/queue; only selected B dispatches."""
import hashlib,json,math,os,random,sys,time
from datetime import datetime,timedelta,timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .clamav_scanner import run_bounded
from .musicbrainz import MusicBrainzProvider,ProviderUnavailable,query_metadata,snapshot,url_for

LOCK_SQL="SELECT pg_try_advisory_lock(hashtextextended(current_database()||':'||'shadify_mb_rate'::regclass::oid::text||':musicbrainz-dispatch',0))"
POSITIVE_TTL=86400;NEGATIVE_TTL=3600;CACHE_CAPACITY=500;QUEUE_CAPACITY=1000
MAX_ATTEMPTS=5;RETRY_BUDGET=86400;RETENTION=604800;INFLIGHT_SECONDS=14

def canonical_request(metadata):
    query=query_metadata(metadata)
    # Confidence/duration is per upload, never part of reusable public response.
    public={key:query[key] for key in ('title','artist','isrc')};public['duration_ms']=1
    if public['isrc']:public['title']='';public['artist']=''
    request={'resource':'isrc' if public['isrc'] else 'recording','query':public,
             'include':'artist-credits' if public['isrc'] else '', 'page':0,'format_version':1}
    key=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return key,request,query

def retry_seconds(value,now):
    if value=='numeric-overflow':return math.inf
    if type(value) is not str or not 1<=len(value)<=256:return None
    try:
        if value.isdecimal():
            seconds=int(value)
        else:
            date=parsedate_to_datetime(value)
            if date.tzinfo is None:return None
            seconds=max(0,(date-now).total_seconds())
        # Validate representable arithmetic rather than silently clipping delay.
        now+timedelta(seconds=seconds)
        return seconds
    except OverflowError:return math.inf # Valid numeric but unrepresentable: pause, never retry faster.
    except (ValueError,TypeError):return None

class QueuedMusicBrainzProvider:
    def __init__(self,*,connect,user_agent,mode='cache_only'):
        if mode not in ('cache_only','b_live'):raise ValueError('Explicit provider mode invalid')
        MusicBrainzProvider(state_dir='/nonexistent',user_agent=user_agent) # identity validation only
        self.connect=connect;self.user_agent=user_agent;self.mode=mode

    def lookup(self,metadata,*,deadline):
        try:return self._lookup(metadata,deadline=deadline)
        except Exception:raise ProviderUnavailable('Public metadata unavailable') from None

    def _lookup(self,metadata,*,deadline):
        if time.monotonic()>=deadline:raise ProviderUnavailable('Lookup deadline')
        key,request,query=canonical_request(metadata)
        with self.connect() as connection,connection.cursor(row_factory=dict_row) as c:
            row=c.execute('SELECT response,fetched_at,response_status FROM shadify_mb_cache WHERE request_key=%s AND expires_at>clock_timestamp()', (key,)).fetchone()
            if row:
                response=row['response']
                result=snapshot(json.dumps(response['document'],separators=(',',':')).encode(),query)
                result.update(fetched_at=row['fetched_at'].isoformat(),snapshot_sha256=response['response_sha256'],response_status=row['response_status'],cache_hit=True)
                return result
            if self.mode=='cache_only':raise ProviderUnavailable('Cache miss; live dispatch disabled')
            # One short singleton lock bounds admission under concurrent misses.
            c.execute('SELECT singleton FROM shadify_mb_rate WHERE singleton FOR UPDATE')
            row=c.execute('SELECT response,fetched_at,response_status FROM shadify_mb_cache WHERE request_key=%s AND expires_at>clock_timestamp()', (key,)).fetchone()
            if row:
                result=snapshot(json.dumps(row['response']['document'],separators=(',',':')).encode(),query)
                result.update(fetched_at=row['fetched_at'].isoformat(),snapshot_sha256=row['response']['response_sha256'],response_status=row['response_status'],cache_hit=True)
                return result
            self._prune(c)
            existing=c.execute('SELECT request_key FROM shadify_mb_queue WHERE request_key=%s',(key,)).fetchone()
            if existing is None and c.execute('SELECT count(*) AS n FROM shadify_mb_queue').fetchone()['n']>=QUEUE_CAPACITY:
                raise ProviderUnavailable('Queue capacity')
            c.execute("INSERT INTO shadify_mb_queue(request_key,request) VALUES(%s,%s) ON CONFLICT(request_key) DO UPDATE SET state='queued',attempts=0,next_at=clock_timestamp(),created_at=clock_timestamp(),updated_at=clock_timestamp(),last_status=NULL,last_error=NULL WHERE shadify_mb_queue.state='done'",(key,Jsonb(request)))
        raise ProviderUnavailable('Public request queued')

    def _prune(self,c):
        c.execute('DELETE FROM shadify_mb_cache WHERE expires_at<=clock_timestamp()')
        c.execute("DELETE FROM shadify_mb_queue WHERE state IN ('done','paused') AND updated_at<clock_timestamp()-%s*interval '1 second'",(RETENTION,))

    def _transport(self,request,*,deadline):
        code,out,err=run_bounded([sys.executable,'-m','shadify_api.musicbrainz','--response','--query',json.dumps(request['query'],separators=(',',':')),
            '--user-agent',self.user_agent,'--parent-pid',str(os.getpid()),'--request-deadline',str(deadline)],deadline=deadline,output_bytes=65536)
        if code or err:raise ProviderUnavailable('Bounded transport unavailable')
        return json.loads(out)

    def dispatch_once(self):
        if self.mode!='b_live':return None
        # This session is autocommit: lifetime exclusion is not a DB transaction.
        with self.connect() as connection:
            connection.commit();connection.autocommit=True
            with connection.cursor(row_factory=dict_row) as c:
                if not c.execute(LOCK_SQL).fetchone()['pg_try_advisory_lock']:return None
                with connection.transaction():
                    rate=c.execute('SELECT *,clock_timestamp() AS now FROM shadify_mb_rate WHERE singleton FOR UPDATE').fetchone()
                    now=rate['now']
                    if rate['paused'] or now<max(rate['next_at'],rate['cooldown_until']) or (rate['inflight_until'] and now<rate['inflight_until']):return None
                    if rate['inflight_until']:
                        # Unknown prior outcome consumes budget and never becomes no-match.
                        c.execute("UPDATE shadify_mb_queue SET state=CASE WHEN attempts>=5 THEN 'paused' ELSE 'queued' END,next_at=clock_timestamp()+interval '30 seconds',last_error='provider_unavailable',updated_at=clock_timestamp() WHERE request_key=%s AND state='inflight'",(rate['request_key'],))
                    self._prune(c)
                    c.execute("UPDATE shadify_mb_queue SET state='paused',last_error='retry_budget_exhausted',updated_at=clock_timestamp() WHERE state='queued' AND (attempts>=5 OR created_at<clock_timestamp()-interval '24 hours')")
                    job=c.execute("SELECT * FROM shadify_mb_queue WHERE state='queued' AND next_at<=clock_timestamp() ORDER BY next_at,created_at,request_key FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
                    if job is None:
                        c.execute('UPDATE shadify_mb_rate SET inflight_until=NULL,request_key=NULL,attempt_id=NULL WHERE singleton');return None
                    dispatch_deadline=time.monotonic()+12
                    attempt=uuid4()
                    c.execute('UPDATE shadify_mb_rate SET inflight_until=clock_timestamp()+%s*interval \'1 second\',request_key=%s,attempt_id=%s WHERE singleton',(INFLIGHT_SECONDS,job['request_key'],attempt))
                    c.execute("UPDATE shadify_mb_queue SET state='inflight',attempts=attempts+1,updated_at=clock_timestamp() WHERE request_key=%s",(job['request_key'],))
                # No file attempt/lease or open DB transaction exists here.
                try:response=self._transport(job['request'],deadline=dispatch_deadline)
                except Exception:response={'status':None,'retry_after':None}
                with connection.transaction():
                    rate=c.execute('SELECT *,clock_timestamp() AS now FROM shadify_mb_rate WHERE singleton FOR UPDATE').fetchone()
                    if rate['attempt_id']!=attempt:raise ProviderUnavailable('Dispatch reservation changed')
                    status=response.get('status');now=rate['now'];success=False
                    if type(status) is int and status in (200,404):
                        try:
                            document=response['document'];digest=response['response_sha256']
                            if type(digest) is not str or len(digest)!=64 or any(v not in '0123456789abcdef' for v in digest):raise ValueError()
                            raw=json.dumps(document,separators=(',',':')).encode()
                            snapshot(raw,job['request']['query'])
                            if len(raw)>49152 or status==404 and (not job['request']['query']['isrc'] or document['recordings']):raise ValueError()
                            self._prune(c)
                            if c.execute('SELECT count(*) AS n FROM shadify_mb_cache').fetchone()['n']>=CACHE_CAPACITY:
                                c.execute('DELETE FROM shadify_mb_cache WHERE request_key=(SELECT request_key FROM shadify_mb_cache ORDER BY fetched_at,request_key LIMIT 1)')
                            ttl=NEGATIVE_TTL if status==404 else POSITIVE_TTL
                            c.execute('INSERT INTO shadify_mb_cache(request_key,request,response,fetched_at,expires_at,response_status) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(request_key) DO UPDATE SET response=excluded.response,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at,response_status=excluded.response_status',
                                (job['request_key'],Jsonb(job['request']),Jsonb({'document':document,'response_sha256':digest}),now,now+timedelta(seconds=ttl),status))
                            success=True
                        except (ValueError,KeyError,TypeError,ProviderUnavailable):pass
                    c.execute('UPDATE shadify_mb_rate SET next_at=clock_timestamp()+interval \'1.1 seconds\',inflight_until=NULL,request_key=NULL,attempt_id=NULL WHERE singleton')
                    if success:
                        c.execute("UPDATE shadify_mb_queue SET state='done',last_status=%s,last_error=NULL,updated_at=clock_timestamp() WHERE request_key=%s",(status,job['request_key']));return 'cached'
                    valid_status=status if type(status) is int and 100<=status<=599 else None
                    attempts=job['attempts']+1
                    delay=min(3600,30*2**(attempts-1))+random.uniform(0,3)
                    server=retry_seconds(response.get('retry_after'),now) if status in (429,503) else None
                    delay=max(delay,server or 0);paused=delay>RETRY_BUDGET
                    # Unsupported numeric delay remains paused indefinitely; this
                    # bounded timestamp is not permission to resume after24h.
                    resume=now+timedelta(seconds=delay if math.isfinite(delay) else RETRY_BUDGET)
                    if status in (429,503):
                        c.execute('UPDATE shadify_mb_rate SET cooldown_until=%s,paused=%s WHERE singleton',(resume,paused))
                    state='paused' if paused or attempts>=MAX_ATTEMPTS else 'queued'
                    c.execute('UPDATE shadify_mb_queue SET state=%s,next_at=%s,last_status=%s,last_error=%s,updated_at=clock_timestamp() WHERE request_key=%s',
                        (state,resume,valid_status,'server_cooldown_review' if paused else 'retry_budget_exhausted' if attempts>=MAX_ATTEMPTS else 'provider_unavailable',job['request_key']))
                    return 'retry'
