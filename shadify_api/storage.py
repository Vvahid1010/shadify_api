"""Provider boundary. Presigning is local; inspection performs bounded R2 reads."""
from dataclasses import dataclass, field
from typing import Protocol

from .config import StorageConfig


class StorageUnavailable(Exception):
    pass


@dataclass(frozen=True)
class UploadAuthorization:
    url: str = field(repr=False)
    headers: dict[str, str]
    expires_in: int


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str
    prefix: bytes = field(repr=False)


class ObjectStorage(Protocol):
    def authorize_upload(self, key: str, mime: str, size: int) -> UploadAuthorization: ...
    def inspect(self, key: str) -> ObjectInfo: ...


class R2Storage:
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

    def authorize_upload(self, key, mime, size):
        try:
            url = self.client.generate_presigned_url(
                "put_object", Params={"Bucket": self.config.bucket, "Key": key,
                "ContentType": mime, "ContentLength": size, "IfNoneMatch": "*"},
                ExpiresIn=self.config.upload_ttl_seconds, HttpMethod="PUT",
            )
            return UploadAuthorization(url, {"Content-Type": mime, "If-None-Match": "*"},
                                       self.config.upload_ttl_seconds)
        except Exception:
            raise StorageUnavailable("Upload authorization unavailable") from None

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
