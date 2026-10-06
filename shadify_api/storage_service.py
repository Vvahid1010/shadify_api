"""Small app-owned media capability boundary; adapters never decide product access."""
import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID
from .auth import Identity, require_fresh_actor, unique_pairs
from .storage import PlaybackAccess, UploadAuthorization, StorageUnavailable


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


class StorageService:
    def __init__(self, repository, adapters, provider, base_url, signing_key, *, upload_ttl=300, access_ttl=900):
        if provider not in adapters or len(signing_key) < 32:
            raise ValueError("Explicit storage adapter and media signing key required")
        self.repository, self.adapters, self.provider = repository, dict(adapters), provider
        self.base_url, self.signing_key = base_url.rstrip('/'), signing_key
        self.upload_ttl, self.access_ttl = upload_ttl, access_ttl

    def adapter(self, asset):
        try: return self.adapters[asset.storage_provider]
        except KeyError: raise StorageUnavailable("Object provider unavailable") from None

    def issue(self, asset, purpose, actor=None, *, preview=False):
        if purpose != 'put': self.repository.require_ingest_approval(asset)
        now = int(time.time())
        lifetime = self.upload_ttl if purpose == 'put' else self.access_ttl
        if purpose != 'put' and asset.storage_provider == 'local':
            lifetime = min(lifetime, 3600)
        expiry = now + lifetime
        if actor is not None:
            require_fresh_actor(actor)
            expiry = min(expiry, actor.expires_at)
        if purpose == 'put': expiry = min(expiry, int(asset.expires_at.timestamp()))
        claims = dict(v=1, purpose=purpose, asset=str(asset.id), key=asset.key,
                      provider=asset.storage_provider, mime=asset.mime, size=asset.size,
                      exp=expiry, track=str(asset.track_id), preview=preview,
                      actor=asdict(actor) if actor else None)
        body = encoded(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode())
        signature = encoded(hmac.digest(self.signing_key, b'shadify-media-v1.' + body.encode(), 'sha256'))
        return self.base_url + '/media/' + str(asset.id) + '?token=' + body + '.' + signature, expiry

    def validate(self, token, asset_id, method):
        from .media import MediaError
        try:
            if len(token) > 4096: raise ValueError()
            body, signature = token.split('.')
            expected = encoded(hmac.digest(self.signing_key, b'shadify-media-v1.' + body.encode(), 'sha256'))
            if not hmac.compare_digest(signature, expected): raise ValueError()
            decoded = base64.b64decode(body + '=' * (-len(body) % 4), altchars=b'-_', validate=True)
            if encoded(decoded) != body: raise ValueError()
            claims = json.loads(decoded, object_pairs_hook=unique_pairs)
            if (claims['v'] != 1 or type(claims['exp']) is not int or claims['exp'] <= int(time.time())
                    or claims['asset'] != str(asset_id) or claims['purpose'] not in ('put', 'private', 'playback')
                    or ((method == 'PUT') != (claims['purpose'] == 'put'))): raise ValueError()
            actor = Identity(**claims['actor']) if claims['actor'] else None
            if actor is not None: require_fresh_actor(actor)
            if claims['purpose'] in ('put', 'private') and actor is None: raise ValueError()
            asset = self.repository.get(asset_id)
            if (asset is None or any(claims[k] != getattr(asset, field) for k, field in
                    (('key', 'key'), ('provider', 'storage_provider'), ('mime', 'mime'), ('size', 'size')))
                    or claims['track'] != str(asset.track_id)): raise ValueError()
            self.adapter(asset)
            return claims, actor, asset
        except Exception:
            raise MediaError(403, "Media authorization unavailable") from None

    def authorize_asset_upload(self, actor, asset):
        self.adapter(asset)
        url, expiry = self.issue(asset, 'put', actor)
        return UploadAuthorization(url, {'Content-Type': asset.mime, 'If-None-Match': '*'}, expiry - int(time.time()))

    def inspect_asset(self, asset):
        return self.adapter(asset).inspect(asset.key)

    def checksum_asset(self,asset):
        from .local_storage import LocalAdapter
        adapter=self.adapter(asset)
        if not isinstance(adapter,LocalAdapter):raise StorageUnavailable('Local quarantine ingestion required')
        return adapter.checksum(asset.key)

    def require_file(self,asset,fd):
        from .media import MediaError
        identity=self.repository.require_ingest_approval(asset)
        info=os.fstat(fd)
        observed=dict(device=info.st_dev,inode=info.st_ino,size=info.st_size,mtime_ns=info.st_mtime_ns,ctime_ns=info.st_ctime_ns)
        if observed!=identity:raise MediaError(409,'Approved object changed')
        return identity

    def private_access(self, actor, asset_id):
        from .media import MediaError
        asset = self.repository.get_for_actor(actor, asset_id)
        if asset is None or asset.state not in ('uploaded', 'ready'):
            raise MediaError(404, "Media not found")
        self.adapter(asset)
        url, expiry = self.issue(asset, 'private', actor)
        return PlaybackAccess(url, datetime.fromtimestamp(expiry, timezone.utc))

    def playback_access(self, track_id, *, preview=False):
        from .delivery import require_playable
        target = self.repository.get_playback_target(track_id, preview=preview)
        asset = require_playable(target, track_id, preview=preview)
        self.adapter(asset)
        url, expiry = self.issue(asset, 'playback', preview=preview)
        return PlaybackAccess(url, datetime.fromtimestamp(expiry, timezone.utc))

    def authorize_read(self, claims, actor, asset):
        from .delivery import require_playable
        from .media import MediaError
        self.repository.require_ingest_approval(asset)
        if claims['purpose'] == 'playback':
            target = self.repository.get_playback_target(asset.track_id, preview=claims['preview'])
            current = require_playable(target, asset.track_id, preview=claims['preview'])
            if current.id != asset.id: raise MediaError(404, "Media not found")
        else:
            current = self.repository.get_for_actor(actor, asset.id)
            if current is None or current.state not in ('uploaded', 'ready'):
                raise MediaError(404, "Media not found")
