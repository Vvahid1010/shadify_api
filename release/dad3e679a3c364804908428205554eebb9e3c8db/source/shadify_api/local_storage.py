"""Private Linux object adapter. Keys are logical; all opens are descriptor-relative."""
import hashlib
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from .storage import ObjectInfo, StorageUnavailable


class LocalAdapter:
    provider = "local"

    def __init__(self, root):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("Absolute private media root required")
        # No provisioning here. Reject symlinks in every configured path component.
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.root.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            info = os.fstat(fd)
            if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o077:
                raise ValueError("Media root must be owner-only and runtime-owned")
        finally:
            os.close(fd)

    @contextmanager
    def parent(self, key, *, create=False):
        parts = key.split('/')
        if len(key) > 1024 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) or p in ('.', '..') for p in parts):
            raise StorageUnavailable("Invalid object key")
        # Reopen all root components without following links on each operation.
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            components = [(p, False) for p in self.root.parts[1:]] + [(p, create) for p in parts[:-1]]
            for part, provision in components:
                if provision:
                    try: os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError: pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            yield fd, parts[-1]
        except FileExistsError:
            raise
        except OSError:
            raise StorageUnavailable("Local object operation unavailable") from None
        finally:
            os.close(fd)

    @contextmanager
    def stage(self, key, mime):
        with self.parent(key, create=True) as (parent, name):
            temporary = '.upload-' + uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                os.setxattr(fd, 'user.shadify.mime', mime.encode('ascii'))
                yield fd, parent, temporary, name
            finally:
                os.close(fd)
                try: os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError: pass

    def install(self, staged):
        fd, parent, temporary, name = staged
        os.fsync(fd)
        # link+unlink is an exclusive atomic publication of the completed inode;
        # ordinary rename would overwrite and renameat2 is not portable Python.
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        try: os.fsync(parent)
        except OSError:
            os.unlink(name, dir_fd=parent)
            raise

    def remove_installed(self, staged):
        _, parent, _, name = staged
        os.unlink(name, dir_fd=parent)

    def open(self, key):
        with self.parent(key) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid()):
                os.close(fd)
                raise StorageUnavailable("Private regular object required")
            return fd

    def inspect(self, key):
        try:
            fd = self.open(key)
            try:
                return ObjectInfo(os.fstat(fd).st_size, os.getxattr(fd, 'user.shadify.mime').decode('ascii'), os.read(fd, 4096))
            finally: os.close(fd)
        except (OSError, UnicodeError):
            raise StorageUnavailable("Object verification unavailable") from None

    def checksum(self, key):
        fd = self.open(key)
        try:
            digest = hashlib.sha256()
            while block := os.read(fd, 1024 * 1024): digest.update(block)
            return digest.hexdigest()
        finally: os.close(fd)
