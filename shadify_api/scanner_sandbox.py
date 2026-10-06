"""Fixed native scanner namespace; no host credential mounts or fallback."""
import hashlib,json,os,stat
from pathlib import Path

def runtime_inventory(root):
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
        allowed=name=='usr/bin/clamscan' or (name.startswith(('lib/','lib64/','usr/lib/','usr/lib64/')) and '.so' in path.name)
        cache=name=='etc/ld.so.cache'
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not (allowed or cache) or info.st_size>134217728:
            raise ValueError('Unexpected runtime file')
        total+=info.st_size
        if total>536870912:raise ValueError('Runtime closure too large')
        with path.open('rb') as stream:
            magic=stream.read(20)
            if (allowed and not magic.startswith(b'\x7fELF')) or (cache and not magic.startswith(b'glibc-ld.so.cache')):
                raise ValueError('Runtime is not a native scanner closure')
            stream.seek(0);digest=hashlib.file_digest(stream,'sha256').hexdigest()
        inventory.append((name,'file',info.st_mode&0o777,digest))
    if not (root/'usr/bin/clamscan').is_file():raise ValueError('Scanner missing from closure')
    return hashlib.sha256(json.dumps(inventory,separators=(',',':')).encode()).hexdigest()

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
        for family in ('main','daily','bytecode'):
            for suffix in ('cvd','cld'):
                source=Path(definitions)/(family+'.'+suffix)
                if source.exists():argv+=['--ro-bind',str(source),'/definitions/'+source.name]
        argv+=['--remount-ro','/definitions']
        if lock_fd is not None:argv+=['--sync-fd',str(lock_fd)] # retained by namespace PID1, closed before payload exec
        return argv+['--','/usr/bin/clamscan',*options]
