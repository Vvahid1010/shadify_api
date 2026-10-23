"""Bounded one-shot ClamAV adapter. Never installs tools or updates signatures."""
import hashlib,json,os,re,selectors,signal,stat,subprocess,time,tempfile
from pathlib import Path
from .ingest_scanner import ScanVerdict
from .scanner_sandbox import public_trust_snapshot

class EvidenceInvalid(Exception):pass

def run_bounded(argv,*,deadline,output_bytes,stdin=None,pass_fds=(),stdin_bytes=None,allow_early_stdin_close=False):
    """No shell; a fresh process group is killed/reaped on every failure."""
    input_identity=None
    if allow_early_stdin_close:
        info=os.fstat(stdin) if stdin is not None else None
        if info is None or not stat.S_ISREG(info.st_mode) or type(stdin_bytes) is not int or stdin_bytes!=info.st_size:
            raise EvidenceInvalid('Probe input limit')
        identity=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)
        input_identity=identity(info)
    process=subprocess.Popen(argv,stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,pass_fds=pass_fds,close_fds=True,start_new_session=True,
        env={'PATH':'/usr/bin:/bin','LC_ALL':'C','LANG':'C'})
    streams={};total=0;pending=b'';input_read=0;input_closed_early=False
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout,process.stderr):
                os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ);streams[stream]=bytearray()
            if stdin is not None:
                os.set_blocking(process.stdin.fileno(),False);selector.register(process.stdin,selectors.EVENT_WRITE)
            while selector.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('Scanner deadline')
                for key,_ in selector.select(min(remaining,.1)):
                    if key.fileobj is process.stdin:
                        if not pending:
                            pending=os.read(stdin,65536);input_read+=len(pending)
                            if stdin_bytes is None or input_read>stdin_bytes:raise EvidenceInvalid('Scanner input limit')
                        if not pending:selector.unregister(process.stdin);process.stdin.close();continue
                        try:count=os.write(process.stdin.fileno(),pending)
                        except BrokenPipeError:
                            if not allow_early_stdin_close:raise EvidenceInvalid('Scanner closed input early') from None
                            # Informational ffprobe may finish before EOF. Keep
                            # draining bounded output and require successful exit;
                            # scan/full decode keep the strict default above.
                            selector.unregister(process.stdin);process.stdin.close()
                            pending=b'';input_closed_early=True;continue
                        pending=pending[count:];continue
                    block=os.read(key.fileobj.fileno(),4096)
                    if not block:selector.unregister(key.fileobj);continue
                    total+=len(block)
                    if total>output_bytes:raise EvidenceInvalid('Scanner output limit')
                    streams[key.fileobj].extend(block)
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Scanner deadline')
            try:code=process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:raise TimeoutError('Scanner deadline') from None
        if input_identity is not None and identity(os.fstat(stdin))!=input_identity:raise EvidenceInvalid('Probe input changed')
        if input_closed_early and (code or streams[process.stderr]):raise EvidenceInvalid('Probe closed input without successful result')
        return code,bytes(streams[process.stdout]),bytes(streams[process.stderr])
    except BaseException:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait()
        raise
    finally:
        process.stdout.close();process.stderr.close()
        if process.stdin is not None and not process.stdin.closed:process.stdin.close()

def definitions_snapshot(directory,*,deadline):
    """Fingerprint all selected official definitions, not just daily's version."""
    directory=Path(directory);items=[];size=0;families=set()
    for path in sorted(directory.iterdir()):
        if path.name in ('freshclam.dat','mirrors.dat') or path.suffix=='.tmp':continue
        if re.fullmatch(r'(main|daily|bytecode)-[1-9][0-9]{0,9}\.cvd\.sign',path.name):continue
        if re.fullmatch(r'(main|daily|bytecode)\.(cvd|cld)',path.name) is None:raise EvidenceInvalid('Unknown definitions inventory')
        family=path.stem
        if family in families:raise EvidenceInvalid('Duplicate definitions')
        families.add(family);fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            before=os.fstat(fd);size+=before.st_size
            if not stat.S_ISREG(before.st_mode) or before.st_size<512 or size>536870912:raise EvidenceInvalid('Definitions bounds')
            header=os.read(fd,512).decode('ascii').strip();parts=header.split(':')
            if len(parts)<9 or parts[0]!='ClamAV-VDB' or not parts[2].isdigit():raise EvidenceInvalid('Definitions version missing')
            os.lseek(fd,0,os.SEEK_SET);digest=hashlib.sha256();read_size=0
            while block:=os.read(fd,1048576):
                if time.monotonic()>=deadline:raise TimeoutError('Definitions deadline')
                read_size+=len(block)
                if read_size>before.st_size:raise EvidenceInvalid('Definitions grew')
                digest.update(block)
            after=os.fstat(fd)
            if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise EvidenceInvalid('Definitions changed')
            items.append((path.name,parts[2],digest.hexdigest(),before.st_ino,before.st_mtime_ns,before.st_ctime_ns))
        finally:os.close(fd)
    if not {'main','daily'}<=families:raise FileNotFoundError('Official definitions missing')
    fingerprint=hashlib.sha256(json.dumps(items,separators=(',',':')).encode()).hexdigest()
    return items,'official-sha256:'+fingerprint

def parse_scan(code,stdout,stderr,*,engine,sha256):
    if stderr:raise EvidenceInvalid('Scanner diagnostics')
    try:text=stdout.decode('ascii')
    except UnicodeError:raise EvidenceInvalid('Scanner encoding') from None
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if len(lines)<7 or lines.count('----------- SCAN SUMMARY -----------')!=1:raise EvidenceInvalid('Incomplete report')
    separator=lines.index('----------- SCAN SUMMARY -----------')
    if separator<1:raise EvidenceInvalid('Unexpected scan results')
    results=lines[:separator]
    hashes=[line for line in results if ' FileHash:' in line]
    if engine!='1.5.4' or len(hashes)!=1 or re.fullmatch(r'/scratch/[A-Za-z0-9_.-]+ FileHash: '+re.escape(sha256)+r' \(sha2-256\)',hashes[0]) is None:
        raise EvidenceInvalid('Exact input hash missing or mismatched')
    results.remove(hashes[0])
    report={}
    allowed={'Known viruses','Engine version','Scanned directories','Scanned files','Infected files','Total errors',
             'Data scanned','Data read','Time','Start Date','End Date'}
    for line in lines[separator+1:]:
        key,sep,value=line.partition(':')
        if not sep or key not in allowed or key in report:raise EvidenceInvalid('Unexpected report')
        report[key]=value.strip()
    if not {'Known viruses','Engine version','Scanned files','Infected files','Data scanned','Data read','Time'}<=report.keys():raise EvidenceInvalid('Partial report')
    if (not report['Known viruses'].isdigit() or int(report['Known viruses'])<=0 or
        report['Engine version']!=engine or report.get('Total errors','0')!='0'):
        raise EvidenceInvalid('Unknown scan coverage')
    if code==0 and results==['stdin: OK'] and report['Infected files']=='0' and report['Scanned files']=='2':return 'clean'
    if code==1 and all(re.fullmatch(r'stdin: [A-Za-z0-9_.-]+ FOUND',line) for line in results) and report['Infected files']=='1' and report['Scanned files']=='1':
        if any('Heuristics.' in line for line in results):raise EvidenceInvalid('Incomplete or heuristic coverage')
        return 'infected'
    raise EvidenceInvalid('Unknown scanner result')

class ClamAVScanner:
    def __init__(self,*,executable,prlimit,definitions,temp_dir,engine_version,limits,lock_fd=None,sandbox=None):
        self.executable=str(executable);self.prlimit=str(prlimit);self.definitions=str(definitions)
        self.temp_dir=str(temp_dir);self.engine_version=engine_version;self.limits=limits;self.lock_fd=lock_fd;self.sandbox=sandbox

    def scan(self,fd,*,sha256,deadline=None):
        own_deadline=time.monotonic()+self.limits.wall_seconds
        deadline=own_deadline if deadline is None else min(deadline,own_deadline)
        try:
            if self.sandbox is None:raise FileNotFoundError('Mandatory scanner sandbox missing')
            if not all(Path(p).is_file() and os.access(p,os.X_OK) for p in (self.executable,self.prlimit)):
                raise FileNotFoundError("Scanner executable unavailable")
            size=os.fstat(fd).st_size
            if size<=0 or size>self.limits.max_input_bytes:raise EvidenceInvalid('Input bounds')
            before,version=definitions_snapshot(self.definitions,deadline=deadline)
            def trust_snapshot():
                try:return public_trust_snapshot(self.sandbox.runtime_root,self.definitions,deadline=deadline)
                except ValueError:raise EvidenceInvalid('Public CVD trust invalid') from None
            trust_before,trust_fingerprint=trust_snapshot()
            version='official-sha256:'+hashlib.sha256((version+':'+trust_fingerprint).encode()).hexdigest()
            limits=[self.prlimit,'--cpu='+str(self.limits.cpu_seconds),'--as='+str(self.limits.memory_bytes),
                    '--fsize='+str(self.limits.max_scan_bytes),'--nofile=128','--']
            inherited=() if self.lock_fd is None else (self.lock_fd,)
            with tempfile.TemporaryDirectory(prefix='shadify-scan-',dir=self.temp_dir) as workspace:
                def run(options,stdin=None):
                    command=self.sandbox.argv(definitions=self.definitions,workspace=workspace,options=options,lock_fd=self.lock_fd,scratch_bytes=self.limits.max_scan_bytes)
                    return run_bounded(limits+command,deadline=deadline,output_bytes=self.limits.output_bytes,
                                       stdin=stdin,pass_fds=inherited,stdin_bytes=size)
                code,out,err=run(['--database=/definitions','--version'])
                if code or err or re.fullmatch(rb'ClamAV '+re.escape(self.engine_version.encode())+rb'/[0-9]+/[^\r\n]+\n?',out) is None:
                    raise EvidenceInvalid('Scanner version unavailable')
                os.lseek(fd,0,os.SEEK_SET)
                code,out,err=run(['--database=/definitions','--tempdir=/scratch','--log-hash','--hash-alg=sha2-256',
                    '--official-db-only=yes','--disable-cache','--allmatch=yes','--heuristic-alerts=yes',
                    '--alert-exceeds-max=yes','--alert-encrypted=yes','--alert-broken=yes',
                    '--max-filesize='+str(self.limits.max_input_bytes),'--max-scansize='+str(self.limits.max_scan_bytes),
                    '--max-scantime=0','--bytecode-timeout='+str(self.limits.wall_seconds*1000),
                    '--pcre-max-filesize='+str(self.limits.max_input_bytes),
                    '--max-htmlnormalize='+str(self.limits.max_input_bytes),'--max-htmlnotags='+str(self.limits.max_input_bytes),
                    '--max-embeddedpe='+str(self.limits.max_input_bytes),'--max-scriptnormalize='+str(self.limits.max_input_bytes),'-'],stdin=fd)
                if os.lseek(fd,0,os.SEEK_CUR)!=size:raise EvidenceInvalid('Partial input read')
                verdict=parse_scan(code,out,err,engine=self.engine_version,sha256=sha256)
            after,_=definitions_snapshot(self.definitions,deadline=deadline)
            if after!=before or trust_snapshot()[0]!=trust_before:raise EvidenceInvalid('Definitions or public trust changed')
            return ScanVerdict(verdict,sha256,self.engine_version,version,full_coverage=True)
        except TimeoutError:return ScanVerdict('unknown',sha256,error_code='scanner_timeout')
        except FileNotFoundError:return ScanVerdict('unknown',sha256,error_code='scanner_unavailable')
        except EvidenceInvalid:return ScanVerdict('unknown',sha256,error_code='scanner_evidence_invalid')
        except Exception:return ScanVerdict('unknown',sha256,error_code='scanner_error')
