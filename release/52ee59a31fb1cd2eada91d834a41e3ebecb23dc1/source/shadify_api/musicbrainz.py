"""Bounded public metadata ONLY. Fixed host, no credentials, uploads or redirects."""
import argparse,hashlib,json,os,re,sys,tempfile,time,unicodedata,urllib.parse,urllib.request,urllib.error
from datetime import datetime,timezone
from pathlib import Path
from uuid import UUID
from .clamav_scanner import run_bounded

class ProviderUnavailable(Exception):pass

def text(value,limit):
    if type(value) is not str or len(value)>limit or any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError('Invalid metadata')
    return value.strip()

def normalized(value):return ' '.join(unicodedata.normalize('NFKC',value).casefold().split())

def query_metadata(metadata):
    # Only public musical identifiers/names; never account/asset/key/hash/file fields.
    result={key:text(metadata.get(key,''),limit) for key,limit in [('title',200),('artist',120),('isrc',17)]}
    result['isrc']=result['isrc'].upper().replace('-','')
    if result['isrc'] and re.fullmatch(r'[A-Z]{2}[A-Z0-9]{3}[0-9]{7}',result['isrc']) is None:result['isrc']=''
    duration=metadata.get('duration_ms')
    if type(duration) is not int or not 1<=duration<=3600000:raise ValueError('Duration unavailable')
    result['duration_ms']=duration
    if not result['isrc'] and (not result['title'] or not result['artist']):raise ProviderUnavailable('Public lookup metadata incomplete')
    return result

def url_for(query):
    if query['isrc']:return 'https://musicbrainz.org/ws/2/isrc/'+query['isrc']+'?inc=artist-credits&fmt=json'
    def quote(value):return '"'+value.replace('\\','\\\\').replace('"','\\"')+'"'
    search='recording:'+quote(query['title'])+' AND artist:'+quote(query['artist'])
    return 'https://musicbrainz.org/ws/2/recording?'+urllib.parse.urlencode({'query':search,'limit':5,'fmt':'json'})

def snapshot(raw,query):
    if type(raw) is not bytes or len(raw)>1048576:raise ProviderUnavailable('Response bounds')
    try:
        document=json.loads(raw)
        if type(document) is not dict or type(document.get('recordings')) is not list:raise ValueError()
        if query['isrc'] and document.get('isrc')!=query['isrc']:raise ValueError()
        candidates=[]
        for item in document['recordings'][:5]:
            mbid=str(UUID(item['id']));title=text(item['title'],200)
            credits=item['artist-credit']
            if type(credits) is not list or not 1<=len(credits)<=20:raise ValueError()
            artist=text(''.join(text(v.get('name',v.get('artist',{}).get('name','')),120)+text(v.get('joinphrase',''),20) for v in credits),120)
            duration=item.get('length')
            if type(duration) is not int or not 1<=duration<=3600000:duration=None
            candidates.append(dict(mbid=mbid,title=title,artist=artist,duration_ms=duration,isrc=query['isrc']))
        if len({c['mbid'] for c in candidates})!=len(candidates):raise ValueError()
        # Only a complete first-page response can become durable evidence. ISRC
        # lookup may omit count; recording search must supply its total. Never
        # infer uniqueness from a partial nonempty page or a sliced candidate list.
        returned=len(document['recordings'])
        totals=[document[key] for key in ('count','recording-count') if key in document]
        if not totals and not query['isrc']:raise ValueError()
        if any(type(value) is not int or value<0 or value!=returned for value in totals):raise ValueError()
        for key in ('offset','recording-offset'):
            if key in document and (type(document[key]) is not int or document[key]!=0):raise ValueError()
        if returned>5:raise ValueError()
        total=totals[0] if totals else returned
        truncated=False
        # Unique returned ISRC recording + exact normalized names + <=2sec duration.
        automatic=None
        if not truncated and len(candidates)==1 and query['isrc']:
            candidate=candidates[0]
            if (query['title'] and query['artist'] and normalized(candidate['title'])==normalized(query['title'])
                and normalized(candidate['artist'])==normalized(query['artist']) and candidate['duration_ms'] is not None
                and abs(candidate['duration_ms']-query['duration_ms'])<=2000):automatic=candidate['mbid']
        return dict(complete=True,provider='musicbrainz',request_sha256=hashlib.sha256(json.dumps(query,sort_keys=True).encode()).hexdigest(),
            snapshot_sha256=hashlib.sha256(raw).hexdigest(),fetched_at=datetime.now(timezone.utc).isoformat(),
            candidates=candidates,total=total,truncated=truncated,automatic_mbid=automatic)
    except (ValueError,KeyError,TypeError,OverflowError,AttributeError):raise ProviderUnavailable('Invalid provider response') from None

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def fetch(query,user_agent):
    request=urllib.request.Request(url_for(query),headers={'User-Agent':user_agent,'Accept':'application/json','Accept-Encoding':'identity'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(request,timeout=8) as response:
            if response.status!=200 or response.headers.get_content_type()!='application/json' or response.headers.get('Content-Encoding','identity')!='identity':raise ProviderUnavailable()
            raw=response.read(1048577)
    except urllib.error.HTTPError as exc:
        if exc.code!=404 or not query['isrc'] or exc.headers.get_content_type()!='application/json':raise ProviderUnavailable() from None
        raw=exc.read(1048577)
        if len(raw)>1048576 or str(json.loads(raw).get('error','')).lower().rstrip('.')!='not found':raise ProviderUnavailable()
        # Valid fixed ISRC lookup completed with an explicit absent-record response.
        result=snapshot(json.dumps({'isrc':query['isrc'],'recordings':[]}).encode(),query)
        result['snapshot_sha256']=hashlib.sha256(raw).hexdigest();result['response_status']=404
        return result
    return snapshot(raw,query)

class MusicBrainzProvider:
    def __init__(self,*,state_dir,user_agent):
        if re.fullmatch(r'Shadify/[A-Za-z0-9_.-]{1,32} \(https://[A-Za-z0-9./_-]{1,200}\)',user_agent) is None:raise ValueError('Meaningful application version/contact URL required')
        self.state_dir=Path(state_dir);self.user_agent=user_agent;self.not_before=0
    def lookup(self,metadata,*,deadline):
        try:return self._lookup(metadata,deadline=deadline)
        except Exception:raise ProviderUnavailable('Provider unavailable') from None
    def _lookup(self,metadata,*,deadline):
        query=query_metadata(metadata)
        # Caller holds lifetime flock; persisted timestamp survives worker restart.
        path=self.state_dir/'musicbrainz-rate.json';last=0
        if path.exists():
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                stat=os.fstat(fd)
                if stat.st_uid!=os.geteuid() or stat.st_mode&0o077 or stat.st_size>128:raise ProviderUnavailable('Rate state invalid')
                saved=json.loads(os.read(fd,128));boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                if saved['boot']==boot:last=float(saved['monotonic'])
            finally:os.close(fd)
        wait=max(0,last+1.1-time.monotonic(),self.not_before-time.monotonic())
        if wait>2 or time.monotonic()+wait>=deadline:raise ProviderUnavailable('Rate budget unavailable')
        if wait:time.sleep(wait)
        now=time.monotonic();self.not_before=now+1.1
        self._reserve_rate(path,now)
        try:
            code,out,err=run_bounded([sys.executable,'-m','shadify_api.musicbrainz','--query',json.dumps(query,separators=(',',':')),'--user-agent',self.user_agent,'--parent-pid',str(os.getpid()),'--request-deadline',str(min(deadline,time.monotonic()+12))],
                deadline=min(deadline,time.monotonic()+12),output_bytes=65536)
            if code or err:raise ProviderUnavailable('Provider unavailable')
            result=json.loads(out)
            if result.get('complete') is not True:raise ProviderUnavailable()
            return result
        except Exception:raise ProviderUnavailable('Provider unavailable') from None


    def _reserve_rate(self,path,now):
        # Caller holds the lifetime flock. Never truncate committed rate state:
        # a crash preserves the previous complete stamp or the new complete stamp.
        # No request starts until both the replacement and its directory are durable.
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        fd=None;pending=None
        try:
            fd,pending=tempfile.mkstemp(prefix='.musicbrainz-rate-',dir=path.parent)
            data=memoryview(json.dumps({'boot':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'monotonic':now},separators=(',',':')).encode())
            while data:
                written=os.write(fd,data)
                if written<=0:raise OSError('Incomplete rate state write')
                data=data[written:]
            os.fsync(fd);os.close(fd);fd=None
            os.replace(pending,path);pending=None
            os.fsync(parent)
        finally:
            if fd is not None:os.close(fd)
            if pending is not None:os.unlink(pending)
            os.close(parent)

def bind_parent(expected):
    # Register death signal BEFORE checking ppid: death in either ordering cannot
    # let a delayed/orphan child begin an HTTP request after the owner has died.
    import ctypes,signal
    if expected<=1 or ctypes.CDLL(None,use_errno=True).prctl(1,signal.SIGKILL,0,0,0)!=0 or os.getppid()!=expected:
        raise ProviderUnavailable('Provider parent custody invalid')

def bind_request_deadline(deadline):
    import math,signal
    remaining=deadline-time.monotonic()
    if not math.isfinite(remaining) or not 0<remaining<=12:raise ProviderUnavailable('Provider reservation expired')
    # Kernel-default termination independently bounds DNS/HTTP even if the parent
    # loses its DB session or is descheduled after a durable reservation.
    signal.signal(signal.SIGALRM,signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL,remaining)

def fetch_response(query,user_agent):
    request=urllib.request.Request(url_for(query),headers={'User-Agent':user_agent,'Accept':'application/json','Accept-Encoding':'identity'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    status=200
    try:
        with opener.open(request,timeout=8) as response:
            if response.status!=200 or response.headers.get_content_type()!='application/json' or response.headers.get('Content-Encoding','identity')!='identity':raise ProviderUnavailable()
            raw=response.read(1048577)
    except urllib.error.HTTPError as exc:
        if exc.code in (429,503):
            retry=exc.headers.get('Retry-After')
            return {'status':exc.code,'retry_after':retry if type(retry) is str and len(retry)<=256 else 'numeric-overflow' if type(retry) is str and retry.isdecimal() else None}
        if exc.code!=404 or not query['isrc'] or exc.headers.get_content_type()!='application/json':return {'status':exc.code,'retry_after':None}
        raw=exc.read(1048577)
        if len(raw)>1048576 or str(json.loads(raw).get('error','')).lower().rstrip('.')!='not found':raise ProviderUnavailable()
        status=404
    if len(raw)>1048576:raise ProviderUnavailable('Public response bounds')
    result=snapshot(json.dumps({'isrc':query['isrc'],'recordings':[]}).encode() if status==404 else raw,query)
    # Reusable PUBLIC recording response only. Never cache a per-file decision.
    document={'recordings':[{'id':v['mbid'],'title':v['title'],'length':v['duration_ms'],'artist-credit':[{'name':v['artist']}]} for v in result['candidates']],
              'count':result['total']}
    if query['isrc']:document['isrc']=query['isrc']
    if len(json.dumps(document).encode())>49152:raise ProviderUnavailable('Normalized public response bounds')
    return {'status':status,'document':document,'response_sha256':hashlib.sha256(raw).hexdigest(),'retry_after':None}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--query',required=True);parser.add_argument('--user-agent',required=True)
    parser.add_argument('--response',action='store_true');parser.add_argument('--parent-pid',type=int,required=True);parser.add_argument('--request-deadline',type=float,required=True);args=parser.parse_args()
    try:
        bind_parent(args.parent_pid)
        bind_request_deadline(args.request_deadline)
        query=query_metadata(json.loads(args.query))
        print(json.dumps(fetch_response(query,args.user_agent) if args.response else fetch(query,args.user_agent),separators=(',',':')))
    except Exception:raise SystemExit(1) from None # No URLs, metadata, credentials or remote errors logged.

if __name__=='__main__':main()
