"""Provider boundary. Presigning is local; inspection performs bounded R2 reads."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol
import math
import time
from urllib.parse import parse_qs, urlsplit

from .config import StorageConfig


class StorageUnavailable(Exception):
    pass


class StorageAuthorizationExpired(Exception):
    pass


@dataclass(frozen=True)
class UploadAuthorization:
    url: str = field(repr=False)
    headers: dict[str, str]
    expires_in: int


@dataclass(frozen=True)
class PlaybackAccess:
    url: str = field(repr=False)
    expires_at: datetime


class MediaDeliveryProvider(Protocol):
    # Internal server-selected key; domain authorization precedes this call.
    def create_playback_access(self, key: str) -> PlaybackAccess: ...


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str
    prefix: bytes = field(repr=False)


class ObjectStorage(Protocol):
    def authorize_upload(self, key: str, mime: str, size: int) -> UploadAuthorization: ...
    def inspect(self, key: str) -> ObjectInfo: ...


class R2Storage:
    provider = "r2"

    def signing_ttl(self, configured, ttl, deadline):
        selected = min(ttl, configured) if ttl is not None else configured
        if deadline is not None:
            if type(deadline) is not int: raise ValueError('Invalid storage deadline')
            # Ceiling gives the SDK a conservative one-second signing margin.
            # This runs in the signing worker, after any thread-pool wait.
            selected = min(selected, deadline - math.ceil(time.time()))
            if selected <= 0: raise StorageAuthorizationExpired('Media authorization expired')
        return selected

    def check_signed_deadline(self, url, deadline):
        if deadline is None: return
        if int(time.time()) >= deadline: raise StorageAuthorizationExpired('Media authorization expired')
        query = parse_qs(urlsplit(url).query)
        timestamp = datetime.strptime(query['X-Amz-Date'][0], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        if timestamp.timestamp() + int(query['X-Amz-Expires'][0]) > deadline:
            # Refuse to return a URL if SDK work crossed the signing margin.
            raise StorageAuthorizationExpired('Media authorization expired')
    def __init__(self, config: StorageConfig, client=None):
        self.config = config
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client(
                "s3", endpoint_url=config.endpoint_url, region_name="auto",
                aws_access_key_id=config.access_key_id.get_secret_value(),
                aws_secret_access_key=config.secret_access_key.get_secret_value(),
                config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=10,
                              retries={"max_attempts": 2}, s3={"addressing_style": "path"}),
            )
        self.client = client

    def authorize_upload(self, key, mime, size, *, ttl=None, checksum=None, deadline=None):
        try:
            ttl = self.signing_ttl(self.config.upload_ttl_seconds, ttl, deadline)
            params = {"Bucket": self.config.bucket, "Key": key, "ContentType": mime,
                      "ContentLength": size, "IfNoneMatch": "*"}
            if checksum is not None: params['ChecksumSHA256'] = checksum
            url = self.client.generate_presigned_url("put_object", Params=params, ExpiresIn=ttl, HttpMethod="PUT")
            self.check_signed_deadline(url, deadline)
            return UploadAuthorization(url, {"Content-Type": mime, "If-None-Match": "*"},
                                       ttl)
        except StorageAuthorizationExpired:
            raise
        except Exception:
            raise StorageUnavailable("Upload authorization unavailable") from None

    def create_playback_access(self, key, *, ttl=None, method="GET", deadline=None):
        try:
            # Conservative expiry rounded to seconds before SDK signing. This
            # returns no later than signing time + TTL even across a second tick.
            ttl = self.signing_ttl(self.config.playback_url_ttl_seconds, ttl, deadline)
            issued_at = datetime.now(timezone.utc).replace(microsecond=0)
            if method not in ("GET", "HEAD"): raise ValueError()
            url = self.client.generate_presigned_url(
                "head_object" if method == "HEAD" else "get_object", Params={"Bucket": self.config.bucket, "Key": key},
                ExpiresIn=ttl, HttpMethod=method,
            )
            self.check_signed_deadline(url, deadline)
            return PlaybackAccess(url, issued_at + timedelta(seconds=ttl))
        except StorageAuthorizationExpired:
            raise
        except Exception:
            raise StorageUnavailable("Playback authorization unavailable") from None

    def inspect(self, key):
        try:
            head = self.client.head_object(Bucket=self.config.bucket, Key=key)
            response = self.client.get_object(Bucket=self.config.bucket, Key=key, Range="bytes=0-4095")
            body = response["Body"]
            try:
                prefix = body.read(4096)
            finally:
                body.close()
            return ObjectInfo(head["ContentLength"], head.get("ContentType", ""), prefix)
        except Exception:
            raise StorageUnavailable("Object verification unavailable") from None
