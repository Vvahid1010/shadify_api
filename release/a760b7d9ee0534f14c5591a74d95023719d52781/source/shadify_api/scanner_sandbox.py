"""Fixed native scanner namespace; no host credential mounts or fallback."""
import base64,hashlib,json,os,re,stat,time
from pathlib import Path

def runtime_inventory(root,*,native_tools=('clamscan',)):
    if native_tools not in (('clamscan',),('ffmpeg','ffprobe')):raise ValueError('Unsupported native closure')
    root=Path(root)
    if not root.is_absolute() or root.is_symlink():raise ValueError('Protected runtime required')
    inventory=[];total=0
    for path in [root,*sorted(root.rglob('*'))]:
        if len(inventory)>=2048:raise ValueError('Runtime inventory too large')
        info=path.lstat();name=path.relative_to(root).as_posix()
        if info.st_uid!=0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode&0o022):raise ValueError('Runtime must be root owned and sealed')
        if stat.S_ISLNK(info.st_mode):
            target=os.readlink(path)
            if os.path.isabs(target) or not path.resolve().is_relative_to(root):raise ValueError('Runtime symlink escapes')
            inventory.append((name,'link',target));continue
        if stat.S_ISDIR(info.st_mode):
            if name!='.' and not any(name==v or name.startswith(v+'/') for v in ('usr','lib','lib64','etc','proc','dev','scratch','definitions')):
                raise ValueError('Unexpected runtime directory')
            inventory.append((name,'directory',info.st_mode&0o777));continue
        allowed=name in {'usr/bin/'+tool for tool in native_tools} or (name.startswith(('lib/','lib64/','usr/lib/','usr/lib64/')) and '.so' in path.name)
        cache=name=='etc/ld.so.cache'
        public_ca=native_tools==('clamscan',) and name=='etc/clamav/certs/clamav.crt'
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not (allowed or cache or public_ca) or info.st_size>134217728:
            raise ValueError('Unexpected runtime file')
        total+=info.st_size
        if total>536870912:raise ValueError('Runtime closure too large')
        with path.open('rb') as stream:
            magic=stream.read(20)
            if (allowed and not magic.startswith(b'\x7fELF')) or (cache and not magic.startswith(b'glibc-ld.so.cache')):
                raise ValueError('Runtime is not a native scanner closure')
            stream.seek(0);digest=hashlib.file_digest(stream,'sha256').hexdigest()
            if public_ca and (info.st_size>65536 or digest!=PUBLIC_CA_SHA256):raise ValueError('Wrong pinned official ClamAV certificate')
        inventory.append((name,'file',info.st_mode&0o777,digest))
    if not all((root/'usr/bin'/tool).is_file() for tool in native_tools):raise ValueError('Native tool missing from closure')
    return hashlib.sha256(json.dumps(inventory,separators=(',',':')).encode()).hexdigest()

PUBLIC_CA_SHA256='60200e4b4b1b1b7257bb4550487aeb142034341f3300a8526e77a3134a940e9d'

def _sealed_public(info):
    if info.st_uid!=0 or info.st_mode&0o022:raise ValueError('Public scanner material must be root owned and sealed')

def _public_directory(directory):
    directory=Path(directory)
    if not directory.is_absolute() or '..' in directory.parts:raise ValueError('Absolute public material path required')
    for path in reversed((directory,*directory.parents)):
        info=path.lstat();_sealed_public(info)
        if not stat.S_ISDIR(info.st_mode):raise ValueError('Public material directory must not be a symlink')
    return directory

def cvd_bindings(definitions):
    directory=_public_directory(definitions);bindings=[]
    for family in ('main','daily','bytecode'):
        path=directory/(family+'.cvd')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            info=os.fstat(fd);_sealed_public(info)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size<512:raise ValueError('Sealed CVD required')
            header=os.read(fd,512).decode('ascii').strip().split(':')
            if len(header)<9 or header[0]!='ClamAV-VDB' or re.fullmatch(r'[1-9][0-9]{0,9}',header[2]) is None:raise ValueError('CVD version invalid')
            bindings.append((path.name,family+'-'+header[2]+'.cvd.sign'))
        finally:os.close(fd)
    # CLD has a different trust/update contract; do not silently infer its support.
    if any(path.suffix=='.cld' for path in directory.iterdir()):raise ValueError('CLD public signature support is not qualified')
    return bindings

def public_trust_snapshot(runtime_root,definitions,*,deadline):
    runtime_root=_public_directory(runtime_root)
    definitions=_public_directory(definitions)
    bindings=cvd_bindings(definitions)
    expected={name for name,_ in bindings}|{signature for _,signature in bindings}
    if {p.name for p in definitions.iterdir()}!=expected:raise ValueError('Definitions/signature inventory differs from CVD families/versions')
    sources={'clamav.crt':runtime_root/'etc/clamav/certs/clamav.crt',
             **{signature:definitions/signature for _,signature in bindings}}
    _public_directory(runtime_root/'etc/clamav/certs')
    items=[];identities=[]
    for name in sorted(sources):
        fd=os.open(sources[name],os.O_RDONLY|os.O_NOFOLLOW)
        try:
            before=os.fstat(fd);_sealed_public(before)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or not 1<=before.st_size<=65536:raise ValueError('Public material bounds invalid')
            if time.monotonic()>=deadline:raise TimeoutError('Public material deadline')
            data=os.read(fd,65537);after=os.fstat(fd)
            identity=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
            if len(data)!=before.st_size or identity(before)!=identity(after):raise ValueError('Public material changed during read')
            digest=hashlib.sha256(data).hexdigest()
            if name=='clamav.crt':
                if digest!=PUBLIC_CA_SHA256:raise ValueError('Wrong pinned official ClamAV certificate')
            else:
                # Framing only. Native libclamav MUST verify the detached signature
                # against the CVD bytes and pinned CA; no application crypto fork.
                match=re.fullmatch(rb'#clamsign-1\.0\n[0-9]+::pkcs7-pem:([A-Za-z0-9+/=]+)\n?',data)
                if match is None:raise ValueError('Malformed detached CVD signature')
                try:der=base64.b64decode(match[1],validate=True)
                except ValueError:raise ValueError('Malformed signature encoding') from None
                if not der.startswith(b'\x30') or len(der)<256:raise ValueError('Malformed signature payload')
            items.append((name,len(data),digest));identities.append((name,*identity(before),digest))
        finally:os.close(fd)
    fingerprint=hashlib.sha256(json.dumps(items,separators=(',',':')).encode()).hexdigest()
    return identities,fingerprint

class NativeSandbox:
    def __init__(self,*,bwrap,runtime_root):
        self.bwrap=str(bwrap);self.runtime_root=str(runtime_root)
    def argv(self,*,definitions,workspace,options,lock_fd=None,scratch_bytes=209715200):
        argv=[self.bwrap,'--unshare-all','--unshare-user','--disable-userns','--assert-userns-disabled',
              '--die-with-parent','--new-session','--cap-drop','ALL','--clearenv',
              '--setenv','PATH','/usr/bin','--setenv','LC_ALL','C','--setenv','LANG','C',
              '--setenv','TZ','UTC','--setenv','HOME','/nonexistent','--setenv','TMPDIR','/scratch',
              '--ro-bind',self.runtime_root,'/','--proc','/proc','--dev','/dev',
              '--size',str(scratch_bytes),'--tmpfs','/scratch','--chdir','/scratch','--size','1048576','--tmpfs','/definitions']
        if definitions is not None:
            for name,signature in cvd_bindings(definitions):
                argv+=['--ro-bind',str(Path(definitions)/name),'/definitions/'+name,
                       '--ro-bind',str(Path(definitions)/signature),'/definitions/'+signature]
        argv+=['--remount-ro','/definitions']
        if lock_fd is not None:argv+=['--sync-fd',str(lock_fd)] # retained by namespace PID1, closed before payload exec
        return argv+['--','/usr/bin/clamscan',*options]
