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

    def issue(self, asset, purpose, actor=None, *, preview=False, quality=None, cover=None):
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
        context=b'shadify-media-v1.'
        if quality is not None:
            if purpose!='playback' or actor is not None or preview or asset.storage_provider!='local':raise ValueError('Full local quality only')
            claims.update(v=2,**{k:str(quality[k]) for k in ('delivery_anchor','source_job','variant_role')})
            context=b'shadify-media-v2.'
        if cover is not None:
            if purpose!='cover' or quality is not None or actor is not None or preview or asset.storage_provider!='local':raise ValueError('Public local PNG cover only')
            resource,content_id=cover
            self.repository.require_public_cover_asset(resource,content_id,asset)
            claims.update(v=3,resource=resource,content_id=str(content_id))
            context=b'shadify-media-v3.'
        elif purpose=='cover':raise ValueError('Named content binding required')
        body = encoded(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode())
        signature = encoded(hmac.digest(self.signing_key, context + body.encode(), 'sha256'))
        return self.base_url + '/media/' + str(asset.id) + '?token=' + body + '.' + signature, expiry

    def validate(self, token, asset_id, method):
        from .media import MediaError
        try:
            if len(token) > 4096: raise ValueError()
            body, signature = token.split('.')
            decoded = base64.b64decode(body + '=' * (-len(body) % 4), altchars=b'-_', validate=True)
            if encoded(decoded) != body: raise ValueError()
            claims = json.loads(decoded, object_pairs_hook=unique_pairs)
            version=claims['v']
            if type(version) is not int or version not in (1,2,3):raise ValueError()
            context={1:b'shadify-media-v1.',2:b'shadify-media-v2.',3:b'shadify-media-v3.'}[version]
            expected=encoded(hmac.digest(self.signing_key,context+body.encode(),'sha256'))
            if not hmac.compare_digest(signature,expected):raise ValueError()
            if version==2:
                fields={'v','purpose','asset','key','provider','mime','size','exp','track','preview','actor','delivery_anchor','source_job','variant_role'}
                if set(claims)!=fields or claims['purpose']!='playback' or claims['provider']!='local' or claims['preview'] is not False or claims['actor'] is not None or method not in ('GET','HEAD') or claims['variant_role'] not in ('high','low'):raise ValueError()
                if type(claims['size']) is not int or claims['size']<=0 or type(claims['key']) is not str or not 1<=len(claims['key'])<=1024 or type(claims['mime']) is not str or not 1<=len(claims['mime'])<=128:raise ValueError()
                for key in ('asset','track','delivery_anchor','source_job'):
                    if type(claims[key]) is not str or str(UUID(claims[key]))!=claims[key]:raise ValueError()
            if version==3:
                fields={'v','purpose','asset','key','provider','mime','size','exp','track','preview','actor','resource','content_id'}
                if set(claims)!=fields or claims['purpose']!='cover' or claims['resource'] not in ('tracks','releases') or claims['provider']!='local' or claims['mime']!='image/png' or claims['actor'] is not None or claims['preview'] is not False or method not in ('GET','HEAD'):raise ValueError()
                for key in ('asset','content_id'):
                    if type(claims[key]) is not str or str(UUID(claims[key]))!=claims[key]:raise ValueError()
                if type(claims['size']) is not int or claims['size']<=0 or type(claims['key']) is not str or not 1<=len(claims['key'])<=1024:raise ValueError()
            elif claims['purpose']=='cover':raise ValueError()
            if (type(claims['exp']) is not int or claims['exp']<=int(time.time()) or claims['asset']!=str(asset_id) or claims['purpose'] not in ('put','private','playback','profile','cover') or ((method=='PUT')!=(claims['purpose']=='put'))):raise ValueError()
            actor = Identity(**claims['actor']) if claims['actor'] else None
            if actor is not None: require_fresh_actor(actor)
            if claims['purpose'] in ('put', 'private') and actor is None: raise ValueError()
            asset = self.repository.get(asset_id)
            if (asset is None or any(claims[k] != getattr(asset, field) for k, field in
                    (('key', 'key'), ('provider', 'storage_provider'), ('mime', 'mime'), ('size', 'size')))
                    or claims['track'] != str(asset.track_id)): raise ValueError()
            if claims['purpose']=='profile' and (actor is not None or claims['preview'] is not False
                    or asset.asset_type not in ('avatar_delivery','header_delivery') or asset.track_id is not None):raise ValueError()
            if version==3 and (asset.asset_type!='artwork_delivery' or asset.mime!='image/png'):raise ValueError()
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
        info=self.adapter(asset).validate_descriptor(fd,asset.key)
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

    def profile_access(self,artist,purpose,*,expected_asset=None):
        asset=self.repository.public_profile_asset(artist,purpose,expected_asset=expected_asset)
        self.repository.require_public_profile_asset(asset)
        self.adapter(asset)
        url,expiry=self.issue(asset,'profile')
        return PlaybackAccess(url,datetime.fromtimestamp(expiry,timezone.utc))

    def cover_access(self,resource,content_id,*,expected_asset=None):
        asset=self.repository.public_cover_asset(resource,content_id,expected_asset=expected_asset)
        self.adapter(asset)
        url,expiry=self.issue(asset,'cover',cover=(resource,content_id))
        self.repository.require_public_cover_asset(resource,content_id,asset)
        return PlaybackAccess(url,datetime.fromtimestamp(expiry,timezone.utc))

    def playback_access(self, track_id, *, preview=False):
        from .delivery import require_playable
        target = self.repository.get_playback_target(track_id, preview=preview)
        asset = require_playable(target, track_id, preview=preview)
        self.adapter(asset)
        url, expiry = self.issue(asset, 'playback', preview=preview)
        return PlaybackAccess(url, datetime.fromtimestamp(expiry, timezone.utc))

    def select_quality(self,track_id,quality,save_data=False):
        from .playback_quality import select
        return select(self,track_id,quality,save_data)

    def authorize_quality(self,selection):
        from .playback_quality import authorize
        authorize(self.repository,{k:str(selection[k]) for k in ('delivery_anchor','source_job','variant_role')},selection['asset'])

    def issue_quality(self,selection):
        self.authorize_quality(selection)
        asset=selection['asset'];adapter=self.adapter(asset);fd=adapter.open(asset.key)
        try:self.require_file(asset,fd)
        finally:os.close(fd)
        self.authorize_quality(selection)
        url,expiry=self.issue(asset,'playback',quality=selection)
        return PlaybackAccess(url,datetime.fromtimestamp(expiry,timezone.utc))

    def authorize_read(self, claims, actor, asset):
        from .delivery import require_playable
        from .media import MediaError
        self.repository.require_ingest_approval(asset)
        if claims['v']==3:
            self.repository.require_public_cover_asset(claims['resource'],UUID(claims['content_id']),asset)
        elif claims['v']==2:
            from .playback_quality import authorize
            authorize(self.repository,claims,asset)
        elif claims['purpose']=='profile':
            self.repository.require_public_profile_asset(asset)
        elif claims['purpose'] == 'playback':
            target = self.repository.get_playback_target(asset.track_id, preview=claims['preview'])
            current = require_playable(target, asset.track_id, preview=claims['preview'])
            if current.id != asset.id: raise MediaError(404, "Media not found")
        else:
            current = self.repository.get_for_actor(actor, asset.id)
            if current is None or current.state not in ('uploaded', 'ready'):
                raise MediaError(404, "Media not found")
