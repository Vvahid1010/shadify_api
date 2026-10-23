"""Protected server-side configuration; never serialize this model to clients."""
import json
import os
import stat
import struct
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class SplitLocalOwnership(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,hide_input_in_errors=True,frozen=True)
    outputs_root:str
    api_uid:int=Field(gt=0,le=4294967294)
    ingest_uid:int=Field(gt=0,le=4294967294)
    media_gid:int=Field(gt=0,le=4294967294)
    @model_validator(mode='after')
    def distinct(self):
        if self.api_uid==self.ingest_uid:raise ValueError('Distinct non-root owners required')
        return self

def split_paths(originals,policy,*,excluded=()):
    roots=[Path(originals),Path(policy.outputs_root)]
    for path in roots:
        if not path.is_absolute() or '..' in path.parts:raise ValueError('Explicit absolute media roots required')
    resolved=[path.resolve() for path in roots]
    if resolved[0].is_relative_to(resolved[1]) or resolved[1].is_relative_to(resolved[0]):raise ValueError('Media roots overlap')
    for value in excluded:
        other=Path(value).resolve()
        if any(path.is_relative_to(other) or other.is_relative_to(path) for path in resolved):raise ValueError('Media roots overlap protected paths')
    return roots

class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: Literal["r2"] = "r2"
    media_base_url: str | None = None
    local_root: str | None = None
    local_split: SplitLocalOwnership | None = None
    endpoint_url: str
    bucket: str = Field(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$")
    access_key_id: SecretStr
    secret_access_key: SecretStr
    upload_ttl_seconds: int = Field(default=300, ge=30, le=900)
    playback_url_ttl_seconds: int = Field(default=900, ge=1, le=604800, strict=True)
    max_upload_bytes: int = Field(default=104857600, gt=0, le=104857600)

    @model_validator(mode="after")
    def validate_r2(self):
        url = urlsplit(self.endpoint_url)
        host = url.hostname or ""
        account = host.removesuffix(".r2.cloudflarestorage.com")
        if (url.scheme != "https" or host != account + ".r2.cloudflarestorage.com"
                or len(account) != 32 or any(c not in "0123456789abcdef" for c in account)
                or url.username or url.password or url.port or url.query or url.fragment
                or url.path not in ("", "/")):
            raise ValueError("Expected the account-scoped HTTPS R2 S3 endpoint")
        for value in (self.access_key_id, self.secret_access_key):
            if not value.get_secret_value().strip() or "PLACEHOLDER" in value.get_secret_value():
                raise ValueError("Storage credentials are not configured")
        return self


SYSTEMD_CREDENTIALS_ROOT = Path("/run/credentials")


def _systemd_projection(path: Path, fd: int, info) -> bool:
    """Recognize the existing root/service-only ACL projection, not group sharing."""
    directory = path.parent
    if (os.environ.get("CREDENTIALS_DIRECTORY") != str(directory)
            or directory.parent != SYSTEMD_CREDENTIALS_ROOT
            or directory.resolve() != directory
            or not directory.name.endswith(".service")
            or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o440):
        return False
    # Linux ACL mode group bits are the named-user mask. The owning group and
    # other users must have no permissions; only root and this service may read.
    def expected_acl(permissions):
        return struct.pack("<I", 2) + b"".join(
            struct.pack("<HHI", tag, bits, identity) for tag, bits, identity in (
                (1, permissions, 0xffffffff), (2, permissions, os.geteuid()),
                (4, 0, 0xffffffff), (16, permissions, 0xffffffff), (32, 0, 0xffffffff)))
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = os.fstat(directory_fd)
            member = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            flags = os.fstatvfs(fd).f_flag
            return (stat.S_IMODE(parent.st_mode) == 0o550
                    and parent.st_uid == 0 and parent.st_gid == 0
                    and (member.st_dev, member.st_ino) == (info.st_dev, info.st_ino)
                    and flags & os.ST_RDONLY and flags & os.ST_NOSUID
                    and os.getxattr(directory_fd, "system.posix_acl_access") == expected_acl(5)
                    and os.getxattr(fd, "system.posix_acl_access") == expected_acl(4))
        finally:
            os.close(directory_fd)
    except OSError:
        return False


def load_protected_json(path: Path) -> dict:
    # Open without following a symlink and check the opened descriptor, avoiding TOCTOU.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode)
                or info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o077 and not _systemd_projection(path, stream.fileno(), info))):
            raise ValueError("Configuration must be an owner-only regular file owned by root or the API user")
        try:
            value = json.load(stream)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except Exception:
            raise ValueError("Protected configuration is invalid; values are redacted") from None


class LocalStorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: Literal["local"]
    local_root: str
    local_split: SplitLocalOwnership | None = None
    media_base_url: str
    upload_ttl_seconds: int = Field(default=300, ge=30, le=900, strict=True)
    playback_url_ttl_seconds: int = Field(default=3600, ge=1, le=3600, strict=True)
    max_upload_bytes: int = Field(default=104857600, gt=0, le=104857600, strict=True)


def load_storage_config(path: Path) -> StorageConfig | LocalStorageConfig:
    try:
        value = load_protected_json(path)
        selected = os.environ.get('STORAGE_PROVIDER', value.get('provider', 'r2'))
        if selected not in ('local', 'r2') or selected != value.get('provider', 'r2'):
            raise ValueError('Provider must match explicit protected configuration')
        return (LocalStorageConfig if selected == 'local' else StorageConfig).model_validate(value)
    except Exception:
        raise ValueError("Storage configuration is invalid; values are redacted") from None


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    dbname: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr
    sslmode: Literal["disable", "require", "verify-ca", "verify-full"] = "verify-full"
    sslrootcert: str | None = None

    @model_validator(mode="after")
    def validate_assignment(self):
        # Explicit structured settings; no inherited libpq service/connection URL.
        if not self.password.get_secret_value().strip():
            raise ValueError("Database credentials are not configured")
        if self.sslmode == "disable" and self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Remote PostgreSQL requires TLS")
        return self

    def connect(self):
        return self._connect(5, 10000, 5000)

    def connect_nomination_receipt(self):
        """Private immutable reader only; ordinary transactions retain their bounds."""
        from ipaddress import ip_address
        try:
            address = '127.0.0.1' if self.host == 'localhost' else str(ip_address(self.host))
        except ValueError:
            # libpq's timeout does not bound DNS or a list of selected hosts.
            # Receipt activation needs one protected numeric endpoint; ordinary
            # transactions retain their existing configured-host behavior.
            raise ValueError("Nomination receipt requires a numeric DB endpoint") from None
        return self._connect(1, 1000, 500, hostaddr=address)

    def _connect(self, connection_seconds, statement_ms, lock_ms, *, hostaddr=None):
        import psycopg
        options = {"host": self.host, "port": self.port, "dbname": self.dbname,
                   "user": self.user, "password": self.password.get_secret_value(),
                   "sslmode": self.sslmode, "connect_timeout": connection_seconds,
                   "options": f"-c statement_timeout={statement_ms} -c lock_timeout={lock_ms}"}
        if hostaddr is not None:
            options["hostaddr"] = hostaddr
        if self.sslrootcert:
            options["sslrootcert"] = self.sslrootcert
        return psycopg.connect(**options)


def load_database_config(path: Path) -> DatabaseConfig:
    try:
        return DatabaseConfig.model_validate(load_protected_json(path))
    except Exception:
        raise ValueError("Database configuration is invalid; values are redacted") from None


def compose_storage(directory, repository):
    """No provisioning, automatic fallback, ambient credentials or cloud probes."""
    import base64
    from .storage_service import StorageService
    from .local_storage import LocalAdapter
    config = load_storage_config(directory / 'storage.credentials.json')
    url = urlsplit(config.media_base_url or '')
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/') or url.port not in (None, 443)):
        raise ValueError('Explicit HTTPS app media origin required')
    keyfile = load_protected_json(directory / 'media-signing.credentials.json')
    if set(keyfile) != {'key'}: raise ValueError('Media signing key required')
    key = base64.b64decode(keyfile['key'], validate=True)
    if len(key) != 32: raise ValueError('256-bit media signing key required')
    adapters = {}
    if config.local_root:
        root = Path(config.local_root)
        source = Path(__file__).resolve().parents[1]
        if not root.is_absolute() or root.resolve().is_relative_to(source):
            raise ValueError('Local media root must be outside source/build')
        if config.local_split:split_paths(root,config.local_split,excluded=(source,directory))
        adapters['local'] = LocalAdapter(root,split=config.local_split)
    if config.local_split and not config.local_root:raise ValueError('Split layout requires originals root')
    if config.provider == 'r2':
        from .storage import R2Storage
        adapters['r2'] = R2Storage(config)
    return StorageService(repository, adapters, config.provider, config.media_base_url, key,
                          upload_ttl=config.upload_ttl_seconds, access_ttl=config.playback_url_ttl_seconds), config
