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
    endpoint_url: str
    bucket: str = Field(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$")
    access_key_id: SecretStr
    secret_access_key: SecretStr
    upload_ttl_seconds: int = Field(default=300, ge=30, le=900)
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


def load_storage_config(path: Path) -> StorageConfig:
    # Open without following a symlink and check the opened descriptor, avoiding TOCTOU.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid not in (0, os.geteuid())):
            raise ValueError("Configuration must be an owner-only regular file owned by root or the API user")
        try:
            return StorageConfig.model_validate(json.load(stream))
        except Exception:
            raise ValueError("Storage configuration is invalid; values are redacted") from None
