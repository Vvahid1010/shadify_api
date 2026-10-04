"""Protected server-side configuration; never serialize this model to clients."""
import json
import os
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: Literal["r2"] = "r2"
    media_base_url: str | None = None
    local_root: str | None = None
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


def load_protected_json(path: Path) -> dict:
    # Open without following a symlink and check the opened descriptor, avoiding TOCTOU.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid not in (0, os.geteuid())):
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
    media_base_url: str
    upload_ttl_seconds: int = Field(default=300, ge=30, le=900, strict=True)
    playback_url_ttl_seconds: int = Field(default=900, ge=1, le=604800, strict=True)
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
        import psycopg
        options = {"host": self.host, "port": self.port, "dbname": self.dbname,
                   "user": self.user, "password": self.password.get_secret_value(),
                   "sslmode": self.sslmode, "connect_timeout": 5,
                   "options": "-c statement_timeout=10000 -c lock_timeout=5000"}
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
        adapters['local'] = LocalAdapter(root)
    if config.provider == 'r2':
        from .storage import R2Storage
        adapters['r2'] = R2Storage(config)
    return StorageService(repository, adapters, config.provider, config.media_base_url, key,
                          upload_ttl=config.upload_ttl_seconds, access_ttl=config.playback_url_ttl_seconds), config
