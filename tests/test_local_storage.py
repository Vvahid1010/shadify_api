"""Actual local byte transfers + disposable SQL; synthetic identity/key/R2 only."""
import asyncio
import base64
import errno
import hashlib
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from fastapi.testclient import TestClient
from authority_fixture import admitted_actor
from shadify_api.config import compose_storage
from shadify_api.domain import DomainService
from shadify_api.local_storage import LocalAdapter
from shadify_api.main import create_app
from shadify_api.media import Asset, MediaError, MediaService
from shadify_api.media_routes import create_media_app, byte_range
from shadify_api.repository import PostgresMediaRepository
from shadify_api.storage import R2Storage, StorageUnavailable, StorageAuthorizationExpired
from shadify_api.storage_service import StorageService
from test_artist_postgres import ArtistPostgresCase
from test_sdk_startup import test_config


class LocalStorageTests(ArtistPostgresCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory(prefix='shadify-media-proof-')
        self.root = Path(self.directory.name)
        self.repo = PostgresMediaRepository(self.connect)
        self.adapter = LocalAdapter(self.root)
        self.storage = StorageService(self.repo, {'local': self.adapter}, 'local',
                                      'https://testserver', b'SYNTHETIC-MEDIA-KEY-NOT-LIVE-00000', access_ttl=3600)
        self.service = MediaService(self.repo, self.storage, 1000000, 300)
        self.actor = 'owner-a'
        self.api = TestClient(create_app(self.service, lambda: admitted_actor(self.actor),
                             repository=self.repo, domain=DomainService(self.repo)))
        self.bytes = TestClient(create_media_app(self.storage))
        self.assign()
        self.track = UUID(self.repo.content_write(admitted_actor('owner-a'), self.artist,
                          'tracks', {'title': 'Local proof'})['content']['id'])
        self.body = b'fLaC' + b'0123456789abcdef'

    def tearDown(self):
        self.directory.cleanup()
        super().tearDown()

    def upload(self):
        response = self.api.post('/api/media/uploads', json=dict(artist_id=str(self.artist),
                                track_id=str(self.track), asset_type='audio_original', mime='audio/flac', size=len(self.body)))
        self.assertEqual(response.status_code, 201, response.text)
        dto = response.json(); asset = self.repo.get(UUID(dto['asset_id']))
        self.assertEqual(asset.storage_provider, 'local')
        self.assertTrue(asset.key.startswith('artists/'))
        return asset, dto['upload']

    def put(self, upload, body=None, **kwargs):
        return self.bytes.put(upload['url'], content=self.body if body is None else body, headers=upload['headers'] | kwargs)

    def complete(self, asset):
        return self.api.post('/api/media/uploads/' + str(asset.id) + '/complete')

    def access(self, asset):
        response = self.api.post('/api/media/' + str(asset.id) + '/access')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['url']

    def cover(self, *, complete=True):
        # Actual valid tiny PNG; no derived-image producer is implied.
        body=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM6sAAAAASUVORK5CYII=')
        response=self.api.post('/api/media/uploads',json=dict(artist_id=str(self.artist),track_id=str(self.track),
            asset_type='artwork_original',mime='image/png',size=len(body)))
        self.assertEqual(response.status_code,201,response.text);dto=response.json()
        self.cover_upload=dto['upload']
        self.assertEqual(self.bytes.put(dto['upload']['url'],content=body,headers=dto['upload']['headers']).status_code,201)
        asset=self.repo.get(UUID(dto['asset_id']))
        if complete:self.assertEqual(self.complete(asset).status_code,200)
        return asset,body

    def test_lost_cover_upload_response_reconciles_completion_before_retry(self):
        asset,body=self.cover(complete=False)  # PUT committed, client treats its reply as unknown.
        path='/api/artists/'+str(self.artist)+'/tracks/'+str(self.track)
        metadata=self.api.get('/api/media/'+str(asset.id)).json()
        self.assertEqual(metadata['state'],'upload_pending')
        # Same asset completion inspects the installed bytes; no second PUT/new ID.
        self.assertEqual(self.complete(asset).json()['state'],'uploaded')
        self.assertEqual(self.complete(asset).json()['state'],'uploaded')  # Lost completion reply reconciles idempotently.
        self.assertEqual(self.api.patch(path,json={'artwork_asset_id':str(asset.id)}).status_code,200)
        self.assertEqual(self.api.get(path).json()['content']['artwork_asset_id'],str(asset.id))
        self.assertEqual(self.bytes.get(self.access(asset)).content,body)
        digest=self.adapter.checksum(asset.key)
        self.assertEqual(self.bytes.put(self.cover_upload['url'],content=body,headers=self.cover_upload['headers']).status_code,409)
        self.assertEqual(self.adapter.checksum(asset.key),digest)

    def test_draft_original_cover_attach_preview_and_other_user_denial(self):
        asset,body=self.cover()
        path='/api/artists/'+str(self.artist)+'/tracks/'+str(self.track)
        result=self.api.patch(path,json={'artwork_asset_id':str(asset.id)})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['content']['artwork_asset_id'],str(asset.id))
        self.assertEqual(self.api.get(path).json()['content']['artwork_asset_id'],str(asset.id))
        self.assertEqual(self.bytes.get(self.access(asset)).content,body)
        self.assertEqual(self.repo.get(asset.id).state,'uploaded')
        self.assertEqual(self.repo.get(asset.id).access_class,'private')
        self.actor='owner-b'
        self.assertEqual(self.api.patch(path,json={'artwork_asset_id':str(asset.id)}).status_code,404)
        self.assertEqual(self.api.post('/api/media/'+str(asset.id)+'/access').status_code,404)

    def test_draft_cover_requires_completion_same_track_and_artwork_kind(self):
        asset,_=self.cover(complete=False)
        path='/api/artists/'+str(self.artist)+'/tracks/'+str(self.track)
        self.assertEqual(self.api.patch(path,json={'artwork_asset_id':str(asset.id)}).status_code,409)
        self.assertEqual(self.complete(asset).status_code,200)
        other=self.api.post('/api/artists/'+str(self.artist)+'/tracks',json={'title':'Other draft'}).json()['track_id']
        other_path='/api/artists/'+str(self.artist)+'/tracks/'+other
        self.assertEqual(self.api.patch(other_path,json={'artwork_asset_id':str(asset.id)}).status_code,404)
        audio,upload=self.upload();self.assertEqual(self.put(upload).status_code,201)
        self.assertEqual(self.complete(audio).status_code,200)
        self.assertEqual(self.api.patch(path,json={'artwork_asset_id':str(audio.id)}).status_code,422)
        self.assertEqual(self.api.patch(path,json={'original_asset_id':str(audio.id)}).status_code,200)

    def test_draft_original_cover_cannot_publish_or_replace_published_cover(self):
        cover,_=self.cover()
        # Explicit synthetic ready audio row for the publication gate only.
        audio=Asset(uuid4(),'owner-a',self.artist,self.track,'delivery/'+uuid4().hex,'audio_delivery','audio/flac',20,
                    datetime.now(timezone.utc)+timedelta(minutes=5),storage_provider='local',state='ready',
                    access_class='free',publish_state='published')
        self.repo.insert(admitted_actor('owner-a'),audio)
        path='/api/artists/'+str(self.artist)+'/tracks/'+str(self.track)
        self.assertEqual(self.api.patch(path,json={'delivery_asset_id':str(audio.id),'artwork_asset_id':str(cover.id)}).status_code,200)
        self.assertEqual(self.api.put(path+'/publication',json={'state':'published'}).status_code,409)
        self.assertEqual(self.api.get(path).json()['content']['publish_state'],'draft')
        self.assertEqual(self.api.patch(path,json={'artwork_asset_id':None}).status_code,200)
        self.assertEqual(self.api.put(path+'/publication',json={'state':'published'}).status_code,200)
        self.assertEqual(self.api.patch(path,json={'artwork_asset_id':str(cover.id)}).status_code,409)
        self.assertIsNone(self.api.get(path).json()['content']['artwork_asset_id'])

    def test_actual_put_complete_private_head_range_repeated_reads(self):
        asset, upload = self.upload()
        checksum = base64.b64encode(hashlib.sha256(self.body).digest()).decode()
        self.assertEqual(self.put(upload, **{'x-amz-checksum-sha256': checksum}).status_code, 201)
        self.assertEqual(self.adapter.checksum(asset.key), hashlib.sha256(self.body).hexdigest())
        self.assertEqual(self.complete(asset).json()['state'], 'uploaded')
        url = self.access(asset)
        self.assertEqual(urlsplit(url).path, '/media/' + str(asset.id))
        for _ in range(2): self.assertEqual(self.bytes.get(url).content, self.body)
        response = self.bytes.get(url, headers={'Range': 'bytes=4-7'})
        self.assertEqual(response.status_code, 206); self.assertEqual(response.content, self.body[4:8])
        self.assertEqual(response.headers['content-range'], 'bytes 4-7/20')
        self.assertEqual(self.bytes.get(url, headers={'Range': 'bytes=-4'}).content, self.body[-4:])
        head = self.bytes.head(url)
        self.assertEqual(head.status_code, 200); self.assertEqual(head.content, b'')
        self.assertEqual(head.headers['content-length'], '20')
        bad = self.bytes.get(url, headers={'Range': 'bytes=99-'})
        self.assertEqual(bad.status_code, 416); self.assertEqual(bad.headers['content-range'], 'bytes */20')
        self.assertEqual(self.bytes.get('/media/' + str(asset.id)).status_code, 403)
        self.assertEqual(self.bytes.get(url).headers['cache-control'], 'private, no-store')
        self.assertEqual(self.repo.get(asset.id).access_class, 'private')
        self.assertEqual(self.repo.get(asset.id).publish_state, 'draft')
        self.assertEqual(self.api.post('/api/tracks/' + str(self.track) + '/playback').status_code, 404)

    def test_local_link_cap_dev_origin_and_shorter_actor_expiry(self):
        from shadify_api.config import LocalStorageConfig
        settings=LocalStorageConfig(provider='local',local_root=str(self.root),media_base_url='https://dev.shadify.org')
        self.assertEqual(settings.playback_url_ttl_seconds,3600)
        self.assertEqual(settings.upload_ttl_seconds,300)
        self.assertEqual(self.storage.upload_ttl,300)
        with self.assertRaises(ValueError):LocalStorageConfig(provider='local',local_root=str(self.root),media_base_url='https://dev.shadify.org',playback_url_ttl_seconds=3601)
        asset,upload=self.upload();self.assertEqual(self.put(upload).status_code,201);self.assertEqual(self.complete(asset).status_code,200)
        self.storage.base_url='https://dev.shadify.org';self.storage.access_ttl=7200
        now=int(time.time())
        with patch('shadify_api.storage_service.time.time',return_value=now):
            url,expiry=self.storage.issue(asset,'private',admitted_actor('owner-a',issued=now,expires=now+7200))
            self.assertEqual(expiry,now+3600)
            _,upload_expiry=self.storage.issue(asset,'put',admitted_actor('owner-a',issued=now,expires=now+7200))
            self.assertLessEqual(upload_expiry,now+300)
            self.assertEqual(urlsplit(url).hostname,'dev.shadify.org')
            self.assertEqual(urlsplit(url).path,'/media/'+str(asset.id))
            self.assertEqual(self.bytes.get(url).status_code,200)
            _,expiry=self.storage.issue(asset,'private',admitted_actor('owner-a',issued=now,expires=now+45))
            self.assertEqual(expiry,now+45)

    def test_renewal_reevaluates_public_and_private_authority(self):
        asset=self.derived();path='/media/v1/tracks/'+str(self.track)+'/access'
        now=int(time.time())
        with patch('shadify_api.storage_service.time.time',return_value=now):first=self.bytes.get(path)
        self.assertEqual(first.status_code,200);self.assertEqual(int(datetime.fromisoformat(first.json()['expires_at']).timestamp()),now+3600)
        with patch('shadify_api.storage_service.time.time',return_value=now+1):second=self.bytes.get(path)
        self.assertEqual(second.status_code,200);self.assertNotEqual(first.json()['url'],second.json()['url'])
        self.assertEqual(urlsplit(first.json()['url']).path,urlsplit(second.json()['url']).path)
        with self.connect() as c:c.execute("UPDATE shadify_tracks SET access_class='paid' WHERE id=%s",(self.track,))
        denied=self.bytes.get(path);self.assertEqual(denied.status_code,403);self.assertEqual(denied.headers['cache-control'],'private, no-store')
        with self.connect() as c:c.execute("UPDATE shadify_artists SET publish_state='suspended' WHERE id=%s",(self.artist,))
        self.assertEqual(self.bytes.get(path).status_code,404)
        original,upload=self.upload();self.assertEqual(self.put(upload).status_code,201);self.assertEqual(self.complete(original).status_code,200)
        access=self.api.post('/api/media/'+str(original.id)+'/access')
        self.assertEqual(access.status_code,200);self.assertEqual(access.headers['cache-control'],'private, no-store')
        self.repo.change_owner(admitted_actor('admin'),self.artist,action='transfer',target='owner-b',expected='owner-a')
        denied=self.api.post('/api/media/'+str(original.id)+'/access')
        self.assertEqual(denied.status_code,404);self.assertEqual(denied.headers['cache-control'],'private, no-store')

    def test_expiry_after_blocked_read_drops_block_before_yield(self):
        asset,upload=self.upload();self.assertEqual(self.put(upload).status_code,201);self.assertEqual(self.complete(asset).status_code,200)
        self.storage.access_ttl=2;now=int(time.time());clock=[now]
        with patch('shadify_api.storage_service.time.time',side_effect=lambda:clock[0]):url=self.access(asset)
        read=os.read
        def delayed(fd,size):
            block=read(fd,size);clock[0]=now+2;return block
        with patch('shadify_api.media_routes.time.time',side_effect=lambda:clock[0]),patch('shadify_api.media_routes.os.read',side_effect=delayed):response=self.bytes.get(url)
        self.assertEqual(response.content,b'');self.assertEqual(response.headers['content-length'],str(len(self.body)))
        self.assertEqual(response.headers['cache-control'],'private, no-store')

    def test_expiry_after_awaited_authority_check_prevents_next_read(self):
        self.body=b'fLaC'+b'x'*(65536*2-4)
        asset,upload=self.upload();self.assertEqual(self.put(upload).status_code,201);self.assertEqual(self.complete(asset).status_code,200)
        self.storage.access_ttl=2;now=int(time.time());clock=[now];ticks=[0.0];reads=[0];checks=[0]
        with patch('shadify_api.storage_service.time.time',side_effect=lambda:clock[0]):url=self.access(asset)
        read=os.read;authorize=self.storage.authorize_read
        def advancing_read(fd,size):
            block=read(fd,size);reads[0]+=1;ticks[0]+=2.;return block
        def delaying_authority(*args):
            authorize(*args);checks[0]+=1
            if checks[0]==2:clock[0]=now+2
        with patch('shadify_api.media_routes.time.time',side_effect=lambda:clock[0]),patch('shadify_api.media_routes.time.monotonic',side_effect=lambda:ticks[0]),patch('shadify_api.media_routes.os.read',side_effect=advancing_read),patch.object(self.storage,'authorize_read',side_effect=delaying_authority):response=self.bytes.get(url)
        self.assertEqual(len(response.content),65536);self.assertEqual(reads[0],1);self.assertEqual(checks[0],2)

    def test_overwrite_race_exclusive_and_no_temporary_leak(self):
        asset, upload = self.upload()
        with ThreadPoolExecutor(2) as pool: results = list(pool.map(lambda _: self.put(upload).status_code, range(2)))
        self.assertEqual(sorted(results), [201, 412])
        self.assertEqual(self.adapter.inspect(asset.key).size, 20)
        self.assertFalse(list(self.root.rglob('.upload-*')))

    def test_upload_truncation_overflow_checksum_and_disk_full_cleanup(self):
        for body, headers, expected in [(self.body[:-1], {}, 422), (self.body+b'x', {}, 422),
                                      (self.body, {'x-amz-checksum-sha256': base64.b64encode(b'x'*32).decode()}, 422)]:
            asset, upload = self.upload()
            self.assertEqual(self.put(upload, body, **headers).status_code, expected)
            self.assertFalse((self.root/asset.key).exists())
        asset, upload = self.upload()
        with patch('shadify_api.media_routes.os.write', side_effect=OSError(errno.ENOSPC, 'SYNTHETIC-SENSITIVE')):
            response = self.put(upload)
        self.assertEqual(response.status_code, 503); self.assertNotIn('SYNTHETIC', response.text)
        self.assertFalse((self.root/asset.key).exists()); self.assertFalse(list(self.root.rglob('.upload-*')))

    def test_expired_tampered_method_asset_bound_and_cross_user(self):
        asset, upload = self.upload()
        self.assertEqual(self.bytes.get(upload['url']).status_code, 403)
        altered = upload['url'].replace('/media/' + str(asset.id), '/media/' + str(uuid4()))
        self.assertEqual(self.bytes.put(altered, content=self.body, headers=upload['headers']).status_code, 403)
        self.assertEqual(self.bytes.put(upload['url']+'x', content=self.body, headers=upload['headers']).status_code, 403)
        with patch('shadify_api.storage_service.time.time', return_value=time.time()+1000):
            self.assertEqual(self.put(upload).status_code, 403)
        self.assertEqual(self.put(upload).status_code, 201); self.assertEqual(self.complete(asset).status_code, 200)
        url = self.access(asset)
        self.actor = 'owner-b'
        self.assertEqual(self.api.post('/api/media/' + str(asset.id) + '/access').status_code, 404)
        self.repo.change_owner(admitted_actor('admin'), self.artist, action='transfer', target='owner-b', expected='owner-a')
        self.assertEqual(self.bytes.get(url).status_code, 404)
        self.assertEqual(self.complete(asset).status_code, 200)  # current new owner, uploader audit independent

    def test_delegate_revoked_during_stream_before_file_commit(self):
        self.repo.set_delegate(admitted_actor('admin'), 'listener', ['manage_content'], expected_revision=self.admin_revision())
        self.actor = 'listener'; asset, upload = self.upload()
        import httpx
        async def proof():
            async def body():
                yield self.body[:4]
                self.repo.set_delegate(admitted_actor('admin'), 'listener', [], expected_revision=self.admin_revision())
                yield self.body[4:]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_media_app(self.storage))) as client:
                return await client.put(upload['url'], content=body(), headers=upload['headers'])
        response = asyncio.run(proof())
        self.assertEqual(response.status_code, 404, response.text)
        self.assertFalse((self.root/asset.key).exists()); self.assertFalse(list(self.root.rglob('.upload-*')))
        self.assertEqual(self.complete(asset).status_code, 404)

    def test_expiry_during_write_and_audit_failure_remove_final_file(self):
        asset, upload = self.upload()
        original = self.adapter.install
        def expired(staged):
            original(staged)
            raise OSError(errno.ENOSPC, 'synthetic final fsync failure')
        # Actual fsync-after-link failure is cleaned by the adapter itself.
        fsync = os.fsync
        def fail_directory(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError(errno.ENOSPC, 'synthetic')
            return fsync(fd)
        with patch('shadify_api.local_storage.os.fsync', side_effect=fail_directory):
            self.assertEqual(self.put(upload).status_code, 503)
        self.assertFalse((self.root/asset.key).exists())
        self.repo.set_delegate(admitted_actor('admin'), 'listener', ['manage_content'], expected_revision=self.admin_revision())
        self.actor = 'listener'; asset, upload = self.upload()
        with self.connect() as c:
            c.execute("CREATE FUNCTION reject_media_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic'; END $$")
            c.execute('CREATE TRIGGER reject_media_audit BEFORE INSERT ON shadify_app_admin_audit FOR EACH ROW EXECUTE FUNCTION reject_media_audit()')
        self.assertEqual(self.put(upload).status_code, 503)
        self.assertFalse((self.root/asset.key).exists())

    def test_identity_expiry_after_install_rolls_back_file_and_audit(self):
        asset, upload = self.upload()
        install = self.adapter.install
        now = int(time.time()); clock = [now]
        def expire(staged):
            install(staged)
            clock[0] = now + 10000
        with patch('shadify_api.auth.time.time', side_effect=lambda: clock[0]), patch.object(self.adapter, 'install', side_effect=expire):
            response = self.put(upload)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertFalse((self.root/asset.key).exists())
        self.assertFalse(list(self.root.rglob('.upload-*')))
        self.assertEqual(self.repo.get(asset.id).state, 'upload_pending')

    def test_expiry_during_stream_removes_partial(self):
        asset, upload = self.upload()
        import httpx
        now = int(time.time()); clock = [now]
        async def proof():
            async def body():
                yield self.body[:4]
                clock[0] = now + 10000
                yield self.body[4:]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_media_app(self.storage))) as client:
                return await client.put(upload['url'], content=body(), headers=upload['headers'])
        with patch('shadify_api.auth.time.time', side_effect=lambda: clock[0]):
            response = asyncio.run(proof())
        self.assertEqual(response.status_code, 403)
        self.assertFalse((self.root/asset.key).exists()); self.assertFalse(list(self.root.rglob('.upload-*')))

    def test_anonymous_projection_issuance_real_database_and_query_denials(self):
        asset = self.derived()
        path = '/media/v1/tracks/' + str(self.track) + '/access'
        response = self.bytes.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertEqual(self.bytes.get(response.json()['url'], headers={'Range': 'bytes=4-7'}).content, self.body[4:8])
        for query in ('?preview=1','?preview=true&preview=false','?account_id=owner-a','?token=forged'):
            self.assertEqual(self.bytes.get(path+query).status_code, 422)
        self.assertEqual(self.bytes.post(path).status_code, 405)
        with self.connect() as c:c.execute("UPDATE shadify_tracks SET access_class='paid' WHERE id=%s", (self.track,))
        self.assertEqual(self.bytes.get(path).status_code, 403)
        preview=self.derived(access='paid',preview=True)
        self.assertEqual(self.bytes.get(path+'?preview=true').status_code, 200)
        with self.connect() as c:c.execute("UPDATE shadify_artists SET publish_state='suspended' WHERE id=%s", (self.artist,))
        self.assertEqual(self.bytes.get(path+'?preview=true').status_code, 404)
        self.assertEqual(self.bytes.get('/api/me').status_code, 404)

    def test_anonymous_original_and_draft_denied_without_identity(self):
        asset, upload=self.upload();self.assertEqual(self.put(upload).status_code,201);self.assertEqual(self.complete(asset).status_code,200)
        path='/media/v1/tracks/'+str(self.track)+'/access'
        self.assertEqual(self.bytes.get(path).status_code,404)
        self.derived()
        with self.connect() as c:c.execute("UPDATE shadify_tracks SET publish_state='draft' WHERE id=%s",(self.track,))
        self.assertEqual(self.bytes.get(path).status_code,404)

    def test_stream_authority_rechecks_time_bound_not_every_chunk(self):
        self.body=b'fLaC'+b'x'*(65536*4-4)
        asset, upload=self.upload();self.assertEqual(self.put(upload).status_code,201);self.assertEqual(self.complete(asset).status_code,200)
        url=self.access(asset)
        with patch.object(self.storage,'authorize_read',wraps=self.storage.authorize_read) as authorize:
            self.assertEqual(self.bytes.get(url).content,self.body)
            self.assertEqual(authorize.call_count,1)

    def test_stream_revocation_rechecked_before_next_chunk_at_two_seconds(self):
        self.body=b'fLaC'+b'x'*(65536*4-4)
        self.repo.set_delegate(admitted_actor('admin'),'listener',['manage_content'], expected_revision=self.admin_revision());self.actor='listener'
        asset, upload=self.upload();self.assertEqual(self.put(upload).status_code,201);self.assertEqual(self.complete(asset).status_code,200)
        url=self.access(asset);clock=[0.0];read=os.read;calls=[0]
        def advancing_read(fd,size):
            value=read(fd,size);calls[0]+=1;clock[0]+=1.0
            if calls[0]==1:self.repo.set_delegate(admitted_actor('admin'),'listener',[], expected_revision=self.admin_revision())
            return value
        with patch('shadify_api.media_routes.time.monotonic',side_effect=lambda:clock[0]), \
             patch('shadify_api.media_routes.os.read',side_effect=advancing_read), \
             patch.object(self.storage,'authorize_read',wraps=self.storage.authorize_read) as authorize:
            response=self.bytes.get(url)
        self.assertEqual(len(response.content),65536*2)
        self.assertEqual(response.headers['content-length'],str(len(self.body)))
        self.assertEqual(authorize.call_count,2)

    def test_r2_queue_delay_and_sdk_delay_never_revive_deadline(self):
        client=Mock()
        adapter=R2Storage(test_config(),client)
        now=int(time.time());deadline=now+10
        for operation in (lambda:adapter.authorize_upload('server/key','audio/flac',20,deadline=deadline),
                          lambda:adapter.create_playback_access('server/key',deadline=deadline)):
            with patch('shadify_api.storage.time.time',return_value=deadline+1):
                with self.assertRaises(StorageAuthorizationExpired):operation()
            client.generate_presigned_url.assert_not_called()
        def delayed_url(*args,**kwargs):
            timestamp=datetime.fromtimestamp(deadline,timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            return 'https://private-r2.invalid/key?X-Amz-Date='+timestamp+'&X-Amz-Expires='+str(kwargs['ExpiresIn'])
        client.generate_presigned_url.side_effect=delayed_url
        with self.assertRaises(StorageAuthorizationExpired):adapter.create_playback_access('server/key',deadline=deadline)

    def test_r2_queued_route_returns_expired_denial_before_sdk_signing(self):
        import shadify_api.media_routes as routes
        client=Mock();self.storage.adapters['r2']=R2Storage(test_config(),client);self.storage.provider='r2'
        asset,upload=self.upload_unchecked();now=int(time.time());clock=[now]
        original=routes.run_in_threadpool
        async def queued(function,*args,**kwargs):
            if getattr(function,'__name__','')=='authorize_upload':clock[0]=now+10000
            return await original(function,*args,**kwargs)
        with patch('shadify_api.storage.time.time',side_effect=lambda:clock[0]),patch.object(routes,'run_in_threadpool',side_effect=queued):
            response=self.put(upload)
        self.assertEqual(response.status_code,403,response.text)
        client.generate_presigned_url.assert_not_called()

    def test_real_sdk_deadlines_are_bounded_without_network(self):
        from urllib.parse import parse_qs
        import socket
        deadline=int(time.time())+60
        with patch('botocore.httpsession.URLLib3Session.send',side_effect=AssertionError('Network prohibited')), \
             patch.object(socket.socket,'connect',side_effect=AssertionError('Network prohibited')):
            adapter=R2Storage(test_config())
            results=[adapter.authorize_upload('server/immutable/key','audio/flac',20,deadline=deadline),
                     adapter.create_playback_access('server/immutable/key',deadline=deadline),
                     adapter.create_playback_access('server/immutable/key',method='HEAD',deadline=deadline)]
        for result in results:
            query=parse_qs(urlsplit(result.url).query)
            signed=datetime.strptime(query['X-Amz-Date'][0],'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
            self.assertLessEqual(int(signed.timestamp())+int(query['X-Amz-Expires'][0]),deadline)

    def test_shared_local_r2_metadata_range_checksum_immutable_contract(self):
        import io
        asset, upload = self.upload()
        self.assertEqual(self.put(upload).status_code, 201)
        local = self.adapter.inspect(asset.key)
        client = Mock()
        client.head_object.return_value = {'ContentLength': len(self.body), 'ContentType': asset.mime}
        client.get_object.return_value = {'Body': io.BytesIO(self.body)}
        r2 = R2Storage(test_config(), client)
        cloud = r2.inspect(asset.key)
        self.assertEqual(local, cloud)
        client.get_object.assert_called_once_with(Bucket='unit-test-private', Key=asset.key, Range='bytes=0-4095')
        client.generate_presigned_url.return_value = 'https://private-r2.invalid/synthetic'
        checksum = base64.b64encode(hashlib.sha256(self.body).digest()).decode()
        auth = r2.authorize_upload(asset.key, asset.mime, asset.size, ttl=15, checksum=checksum)
        self.assertEqual(auth.expires_in, 15)
        params = client.generate_presigned_url.call_args.kwargs
        self.assertEqual(params['Params']['ChecksumSHA256'], checksum)
        self.assertEqual(params['Params']['IfNoneMatch'], '*')
        self.assertEqual(auth.headers['If-None-Match'], upload['headers']['If-None-Match'])
        client.head_object.side_effect = RuntimeError('SYNTHETIC-SENSITIVE')
        with self.assertRaises(StorageUnavailable) as error: r2.inspect(asset.key)
        self.assertNotIn('SYNTHETIC', str(error.exception))
        with self.assertRaises(StorageUnavailable): self.adapter.inspect('originals/missing/master')

    def test_symlink_key_escape_and_existing_legacy_key(self):
        for key in ('../outside', '/absolute', 'a//b', 'a/../b', 'a/./b'):
            with self.assertRaises(StorageUnavailable): self.adapter.inspect(key)
        asset, upload = self.upload()
        (self.root/'artists').symlink_to('/tmp', target_is_directory=True)
        self.assertEqual(self.put(upload).status_code, 503)
        (self.root/'artists').unlink()
        with self.adapter.stage('originals/legacy/asset/master', 'audio/flac') as staged:
            os.write(staged[0], self.body); self.adapter.install(staged)
        self.assertEqual(self.adapter.inspect('originals/legacy/asset/master').prefix, self.body)

    def derived(self, *, access='free', preview=False, provider='local'):
        asset_id = uuid4()
        asset = Asset(asset_id, 'owner-a', self.artist, self.track,
                      f'artists/{self.artist}/tracks/{self.track}/{asset_id}/audio.m4a',
                      'audio_preview' if preview else 'audio_delivery', 'audio/mp4', len(self.body),
                      datetime.now(timezone.utc)+timedelta(minutes=5), storage_provider=provider,
                      state='ready', access_class='free' if preview else access, publish_state='published')
        with self.connect() as c:
            names = tuple(Asset.__dataclass_fields__)
            c.execute('INSERT INTO shadify_media_assets ('+','.join(names)+') VALUES ('+','.join(['%s']*len(names))+')',
                      tuple(getattr(asset, n) for n in names))
            reference = 'preview_asset_id' if preview else 'delivery_asset_id'
            c.execute('UPDATE shadify_tracks SET '+reference+"=%s, publish_state='published',access_class=%s WHERE id=%s", (asset.id, access, self.track))
            c.execute("UPDATE shadify_artists SET publish_state='published' WHERE id=%s", (self.artist,))
        if provider == 'local':
            with self.adapter.stage(asset.key, asset.mime) as staged:
                os.write(staged[0], self.body); self.adapter.install(staged)
        return asset

    def test_actual_db_playback_free_paid_preview_draft_suspension(self):
        asset = self.derived()
        response = self.api.post('/api/tracks/' + str(self.track) + '/playback')
        self.assertEqual(response.status_code, 200, response.text)
        url = response.json()['url']
        self.assertEqual(self.bytes.get(url, headers={'Range': 'bytes=0-3'}).content, self.body[:4])
        with self.connect() as c: c.execute("UPDATE shadify_artists SET publish_state='suspended' WHERE id=%s", (self.artist,))
        self.assertEqual(self.bytes.get(url).status_code, 404)
        with self.connect() as c:
            c.execute("UPDATE shadify_artists SET publish_state='published' WHERE id=%s", (self.artist,))
            c.execute("UPDATE shadify_tracks SET access_class='paid' WHERE id=%s", (self.track,))
        self.assertEqual(self.api.post('/api/tracks/' + str(self.track) + '/playback').status_code, 403)
        preview = self.derived(access='paid', preview=True)
        response = self.api.post('/api/tracks/' + str(self.track) + '/playback?preview=true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.bytes.get(response.json()['url']).content, self.body)
        self.assertNotEqual(preview.id, asset.id)

    def test_default_cutover_keeps_per_object_local_provider_and_stable_urls(self):
        asset, upload = self.upload(); self.assertEqual(self.put(upload).status_code, 201)
        self.assertEqual(self.complete(asset).status_code, 200)
        before = self.access(asset)
        self.storage.adapters['r2'] = R2Storage(test_config(), Mock())
        self.storage.provider = 'r2'
        after = self.access(asset)
        self.assertEqual(urlsplit(before).path, urlsplit(after).path)
        self.assertEqual(self.bytes.get(after).content, self.body)
        self.assertEqual(self.repo.get(asset.id).storage_provider, 'local')
        new_asset, new_upload = self.upload_unchecked()
        self.assertEqual(new_asset.storage_provider, 'r2')
        self.assertEqual(urlsplit(new_upload['url']).hostname, 'testserver')

    def upload_unchecked(self):
        response = self.api.post('/api/media/uploads', json=dict(artist_id=str(self.artist), track_id=str(self.track),
                                asset_type='audio_original', mime='audio/flac', size=len(self.body)))
        self.assertEqual(response.status_code, 201, response.text)
        return self.repo.get(UUID(response.json()['asset_id'])), response.json()['upload']

    def test_r2_synthetic_redirect_uses_same_opaque_contract_and_expiry_bound(self):
        client = Mock()
        def signed_url(*args, **kwargs):
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            return 'https://private-r2.invalid/synthetic?X-Amz-Date=' + stamp + '&X-Amz-Expires=' + str(kwargs['ExpiresIn'])
        client.generate_presigned_url.side_effect = signed_url
        self.storage.adapters['r2'] = R2Storage(test_config(), client); self.storage.provider='r2'
        asset, upload = self.upload_unchecked()
        response = self.bytes.put(upload['url'], content=self.body, headers=upload['headers'], follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        params = client.generate_presigned_url.call_args.kwargs
        self.assertEqual(params['Params']['ContentType'], 'audio/flac')
        self.assertEqual(params['Params']['ContentLength'], len(self.body))
        self.assertEqual(params['Params']['IfNoneMatch'], '*')
        self.assertLessEqual(params['ExpiresIn'], upload['expires_in'])
        ready = self.derived(provider='r2')
        url = self.api.post('/api/tracks/' + str(self.track) + '/playback').json()['url']
        response = self.bytes.get(url, follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertLessEqual(client.generate_presigned_url.call_args.kwargs['ExpiresIn'], self.storage.access_ttl)
        response = self.bytes.head(url, follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(client.generate_presigned_url.call_args.args, ('head_object',))
        self.assertEqual(client.generate_presigned_url.call_args.kwargs['HttpMethod'], 'HEAD')


class LocalConfigurationTests(unittest.TestCase):
    def test_local_factory_never_loads_r2_credentials_or_client(self):
        import json
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as media:
            directory = Path(temporary)
            for name, value in [('storage.credentials.json', dict(provider='local',local_root=media,media_base_url='https://testserver')),
                                ('media-signing.credentials.json', dict(key=base64.b64encode(b'x'*32).decode()))]:
                path=directory/name; path.write_text(json.dumps(value)); path.chmod(0o600)
            with patch('shadify_api.storage.R2Storage', side_effect=AssertionError('R2 prohibited')), patch.dict(os.environ, {'STORAGE_PROVIDER':'local'}):
                storage, config=compose_storage(directory, Mock())
            self.assertEqual(set(storage.adapters), {'local'})
            self.assertEqual(config.playback_url_ttl_seconds,3600)
            self.assertEqual(storage.access_ttl,3600)
            with patch.dict(os.environ, {'STORAGE_PROVIDER':'r2'}):
                with self.assertRaises(ValueError): compose_storage(directory, Mock())

    def test_root_symlink_and_permissions_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'link').symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError): LocalAdapter(root/'link')
            root.chmod(0o755)
            with self.assertRaises(ValueError): LocalAdapter(root)

    def test_range_single_bounded_suffix_open_and_multi_rejection(self):
        self.assertEqual(byte_range('bytes=0-',20),(0,19,206))
        self.assertEqual(byte_range('bytes=-99',20),(0,19,206))
        for header in ('bytes=-0','bytes=5-4','bytes=0-1,4-5','units=1-2','bytes=-'):
            with self.assertRaises(MediaError): byte_range(header,20)
