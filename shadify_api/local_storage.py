"""Descriptor-relative local objects; explicit split ownership never grants delivery."""
import errno
import hashlib
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from .config import SplitLocalOwnership,split_paths
from .storage import ObjectInfo,StorageUnavailable

def no_acl(fd):
    for name in ('system.posix_acl_access','system.posix_acl_default'):
        try:os.getxattr(fd,name)
        except OSError as exc:
            if exc.errno==errno.ENODATA:continue
            raise StorageUnavailable('Filesystem ACL policy unavailable') from None
        raise StorageUnavailable('Unexpected filesystem ACL')

def descriptor_state(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,
            info.st_uid,info.st_gid,info.st_mode,info.st_nlink)

class _Staged(list):
    def __init__(self,fd,parent,temporary,name,adapter,key):
        super().__init__((fd,parent,temporary,name))
        self.adapter,self.key=adapter,key
        self.closed=False;self.installed=False
        info=os.fstat(fd);self.inode=(info.st_dev,info.st_ino)
    def close(self):
        if not self.closed:
            os.close(self[0]);self.closed=True;self[0]=-1

class LocalAdapter:
    provider='local'
    def __init__(self,root,*,split=None,role='api'):
        self.root=Path(root);self.split=split;self.role=role
        if not self.root.is_absolute():raise ValueError('Absolute private media root required')
        if split is None:
            if role!='api':raise ValueError('Split ownership required for distinct role')
            roots=[(self.root,None)]
        else:
            if not isinstance(split,SplitLocalOwnership):raise ValueError('Protected split ownership required')
            if role not in ('api','ingest') or os.geteuid()!=getattr(split,role+'_uid'):raise ValueError('Selected non-root media identity required')
            source=Path(__file__).resolve().parents[1]
            originals,outputs=split_paths(root,split,excluded=(source,))
            roots=[(originals,split.api_uid),(outputs,split.ingest_uid)]
        for path,owner in roots:
            with self._root(path,owner=owner) as fd:
                info=os.fstat(fd)
                if split is None:
                    if info.st_uid not in (0,os.geteuid()) or info.st_mode&0o077:raise ValueError('Media root must be owner-only and runtime-owned')
                else:
                    try:self._directory(fd,owner)
                    except StorageUnavailable:raise ValueError('Split root ownership/mode invalid') from None

    @contextmanager
    def _root(self,path,*,owner=None):
        fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                os.close(fd);fd=child
                info=os.fstat(fd)
                if self.split and info.st_uid not in (0,owner):raise StorageUnavailable('Untrusted media ancestor')
                if self.split and (info.st_mode&0o022) and not (info.st_uid==0 and info.st_mode&stat.S_ISVTX):
                    raise StorageUnavailable('Writable media ancestor')
            yield fd
        finally:os.close(fd)

    def _directory(self,fd,owner):
        info=os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=owner or info.st_gid!=self.split.media_gid or stat.S_IMODE(info.st_mode)!=0o2750:
            raise StorageUnavailable('Split directory policy changed')
        no_acl(fd)

    def _selection(self,key,*,write=False):
        parts=key.split('/')
        if len(key)>1024 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+',p) or p in ('.','..') for p in parts):raise StorageUnavailable('Invalid object key')
        if self.split is None:return self.root,None,parts
        if os.geteuid()!=getattr(self.split,self.role+'_uid'):raise StorageUnavailable('Media process identity changed')
        if parts[0]=='quarantine' and len(parts)>1:
            selected=self.root;owner=self.split.api_uid;writer='api'
        elif parts[0] in ('artists','delivery') and len(parts)>1:
            selected=Path(self.split.outputs_root);owner=self.split.ingest_uid;writer='ingest'
        else:raise StorageUnavailable('Unknown split object namespace')
        if write and self.role!=writer:raise StorageUnavailable('Read-only media namespace')
        return selected,owner,parts

    @contextmanager
    def parent(self,key,*,create=False):
        selected,owner,parts=self._selection(key,write=create)
        fd=None
        try:
            with self._root(selected,owner=owner) as root_fd:
                fd=os.dup(root_fd)
            if self.split:self._directory(fd,owner)
            else:
                info=os.fstat(fd)
                if info.st_uid not in (0,os.geteuid()) or info.st_mode&0o077:raise StorageUnavailable('Private root policy changed')
            for part in parts[:-1]:
                created=False
                if create:
                    try:os.mkdir(part,0o700,dir_fd=fd);created=True
                    except FileExistsError:pass
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                if created and self.split:
                    info=os.fstat(child)
                    if info.st_uid!=owner or info.st_gid!=self.split.media_gid:os.close(child);raise StorageUnavailable('New directory identity invalid')
                    try:no_acl(child);os.fchmod(child,0o2750);os.fsync(child);os.fsync(fd)
                    except BaseException:os.close(child);raise
                os.close(fd);fd=child
                if self.split:self._directory(fd,owner)
            yield fd,parts[-1]
        except FileExistsError:raise
        except OSError:raise StorageUnavailable('Local object operation unavailable') from None
        finally:
            if fd is not None:os.close(fd)

    @contextmanager
    def stage(self,key,mime):
        self._selection(key,write=True)
        with self.parent(key,create=True) as (parent,name):
            temporary='.upload-'+uuid4().hex
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
            staged=_Staged(fd,parent,temporary,name,self,key)
            try:
                os.setxattr(fd,'user.shadify.mime',mime.encode('ascii'))
                yield staged
            finally:
                staged.close()
                try:os.unlink(temporary,dir_fd=parent);os.fsync(parent)
                except FileNotFoundError:pass

    def _staged(self,staged):
        if not isinstance(staged,_Staged) or staged.adapter is not self:raise StorageUnavailable('Unowned staged object')
        self._selection(staged.key,write=True)
        if self.split:
            _,owner,_=self._selection(staged.key);self._directory(staged[1],owner)

    def _remove_same(self,staged):
        info=os.stat(staged[3],dir_fd=staged[1],follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino)!=staged.inode:raise StorageUnavailable('Installed object replaced')
        os.unlink(staged[3],dir_fd=staged[1]);staged.installed=False

    def install(self,staged):
        self._staged(staged)
        if staged.closed or staged.installed:raise StorageUnavailable('Stage already completed')
        fd,parent,temporary,name=staged
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino)!=staged.inode:raise StorageUnavailable('Staged descriptor changed')
            if self.split:
                if info.st_uid!=os.geteuid() or info.st_gid!=self.split.media_gid or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1:raise StorageUnavailable('Private stage policy changed')
                no_acl(fd);os.fchmod(fd,0o640)
            os.fsync(fd)
            os.link(temporary,name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
            staged.installed=True
            os.unlink(temporary,dir_fd=parent)
            os.fsync(parent)
            staged.close()
            # Final object is single-linked and has no retained writer descriptor.
            check=self.open(staged.key)
            try:
                current=os.fstat(check)
                if (current.st_dev,current.st_ino)!=staged.inode or current.st_nlink!=1:raise StorageUnavailable('Installed identity changed')
            finally:os.close(check)
        except BaseException:
            if staged.installed:
                self._remove_same(staged)
                try:os.fsync(parent)
                except OSError:pass
            raise

    def remove_installed(self,staged):
        self._staged(staged);self._remove_same(staged);os.fsync(staged[1])

    def validate_descriptor(self,fd,key):
        _,owner,_=self._selection(key)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):raise StorageUnavailable('Private regular object required')
        if self.split:
            if info.st_uid!=owner or info.st_gid!=self.split.media_gid or stat.S_IMODE(info.st_mode)!=0o640 or info.st_nlink!=1:raise StorageUnavailable('Split object policy changed')
            no_acl(fd)
        elif info.st_mode&0o077 or info.st_uid not in (0,os.geteuid()):raise StorageUnavailable('Private regular object required')
        return info

    def open(self,key):
        with self.parent(key) as (parent,name):
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            try:self.validate_descriptor(fd,key)
            except BaseException:os.close(fd);raise
            return fd

    def inspect(self,key):
        try:
            fd=self.open(key)
            try:return ObjectInfo(os.fstat(fd).st_size,os.getxattr(fd,'user.shadify.mime').decode('ascii'),os.read(fd,4096))
            finally:os.close(fd)
        except (OSError,UnicodeError):raise StorageUnavailable('Object verification unavailable') from None

    def checksum(self,key):
        fd=self.open(key)
        try:
            digest=hashlib.sha256()
            while block:=os.read(fd,1024*1024):digest.update(block)
            return digest.hexdigest()
        finally:os.close(fd)
