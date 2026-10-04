"""Real nginx -> Unix Uvicorn factory -> local bytes/SQL, all allocations disposable.

This verifies the API handler and narrow socket shape, not Node's generated
units/nginx projection, public TLS, deployment or real Authentication admission.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import httpx
from authority_fixture import admitted_actor
from shadify_api.config import compose_storage
from shadify_api.media import Asset, MediaService, UploadIntent
from shadify_api.repository import PostgresMediaRepository
from test_artist_postgres import ArtistPostgresCase


class MediaSocketTests(ArtistPostgresCase):
    def test_real_factory_nginx_unix_upload_completion_ranges_and_guest_denials(self):
        binary=shutil.which('nginx')
        if not binary: self.skipTest('Existing nginx required; no installation or live fallback')
        repo=PostgresMediaRepository(self.connect);self.assign()
        track=UUID(repo.content_write(admitted_actor('owner-a'),self.artist,'tracks',{'title':'Socket proof'})['content']['id'])
        with tempfile.TemporaryDirectory(prefix='shadify-media-socket-') as temporary:
            root=Path(temporary);media=root/'objects';media.mkdir(mode=0o700);credentials=root/'custody';credentials.mkdir(mode=0o700)
            # Test schema selection is explicit in a test-only wrapper connect;
            # media_runtime config uses the actual disposable PostgreSQL DB.
            # Its only search_path setting is fixed by this isolated test role.
            with self.connect() as c:
                c.execute('ALTER ROLE shadify_test SET search_path TO '+self.schema)
            values={'database.credentials.json':dict(host=self.server.connection['host'],dbname='postgres',user='shadify_test',
                                                   password='SYNTHETIC-UNUSED-TRUST-PASSWORD',sslmode='require'),
                    'storage.credentials.json':dict(provider='local',local_root=str(media),media_base_url='https://dev.shadify.org'),
                    'media-signing.credentials.json':dict(key=base64.b64encode(b'Q'*32).decode())}
            for filename,value in values.items():
                file=credentials/filename;file.write_text(json.dumps(value));file.chmod(0o600)
            storage,config=compose_storage(credentials,repo);service=MediaService(repo,storage,config.max_upload_bytes,config.upload_ttl_seconds)
            body=b'fLaC'+b'0123456789abcdef'
            actor=admitted_actor('owner-a')
            asset,upload=service.create(actor,UploadIntent(artist_id=self.artist,track_id=track,asset_type='audio_original',mime='audio/flac',size=len(body)))
            socket=root/'receiver.sock';front=root/'nginx.sock'
            nginx_config=root/'nginx.conf'
            nginx_config.write_text(f"""daemon off;
pid {root}/nginx.pid;
error_log {root}/nginx-error.log warn;
events {{ worker_connections 32; }}
http {{
 access_log off;
 client_body_temp_path {root}/body;
 proxy_temp_path {root}/proxy;
 server {{
  listen unix:{front};
  server_name dev.shadify.org;
  if ($host != dev.shadify.org) {{ return 404; }}
  client_max_body_size 100m;
  location ~ "^/media/[0-9a-f-]{{36}}$" {{
   if ($request_method !~ ^(PUT|GET|HEAD)$) {{ return 405; }}
   proxy_pass http://unix:{socket};
   proxy_http_version 1.1; proxy_request_buffering off; proxy_buffering off; proxy_next_upstream off;
  }}
  location ~ "^/media/v1/tracks/[0-9a-f-]{{36}}/access$" {{
   if ($request_method != GET) {{ return 405; }}
   proxy_pass http://unix:{socket};
   proxy_http_version 1.1; proxy_request_buffering off; proxy_buffering off; proxy_next_upstream off;
  }}
  location / {{ return 404; }}
 }}
}}
""")
            environment=os.environ.copy();environment['CREDENTIALS_DIRECTORY']=str(credentials);environment['STORAGE_PROVIDER']='local'
            environment['PYTHONDONTWRITEBYTECODE']='1'
            import shadify_api
            environment['PYTHONPATH']=str(Path(shadify_api.__file__).resolve().parents[1])
            with (root/'uvicorn.log').open('w') as log:
                api=subprocess.Popen([sys.executable,'-m','uvicorn','shadify_api.media_runtime:app_factory','--factory',
                                      '--uds',str(socket),'--no-access-log'],env=environment,stdout=log,stderr=subprocess.STDOUT)
                proxy=None
                try:
                    self.wait_socket(socket,api,root/'uvicorn.log')
                    validation=subprocess.run([binary,'-t','-p',str(root)+'/', '-c',str(nginx_config)],capture_output=True,text=True)
                    self.assertEqual(validation.returncode,0,validation.stderr)
                    proxy=subprocess.Popen([binary,'-p',str(root)+'/', '-c',str(nginx_config)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    self.wait_socket(front,proxy,root/'nginx-error.log')
                    with httpx.Client(transport=httpx.HTTPTransport(uds=str(front)),base_url='http://dev.shadify.org') as client:
                        def path(url):
                            value=urlsplit(url);return value.path+'?'+value.query
                        response=client.put(path(upload.url),content=iter([body[:4],body[4:]]),headers=upload.headers)
                        self.assertEqual(response.status_code,201,response.text)
                        self.assertEqual(client.put(path(upload.url),content=body,headers=upload.headers).status_code,412)
                        self.assertEqual(service.complete(actor,asset.id).state,'uploaded')
                        access=storage.private_access(actor,asset.id)
                        response=client.get(path(access.url),headers={'Range':'bytes=4-7'})
                        self.assertEqual(response.status_code,206);self.assertEqual(response.content,body[4:8])
                        self.assertEqual(client.head(path(access.url)).headers['content-length'],'20')
                        self.assertEqual(client.get(path(access.url),headers={'Range':'bytes=99-'}).status_code,416)
                        self.assertEqual(client.get('/media/'+str(asset.id)).status_code,403)
                        self.assertEqual(client.get(path(access.url)+'&token=duplicate').status_code,403)
                        self.assertEqual(client.get('/media/v1/tracks/'+str(track)+'/access').status_code,404)
                        self.assertEqual(client.get('/api/me').status_code,404)
                        self.assertEqual(client.get(path(access.url),headers={'Host':'other.invalid'}).status_code,404)
                        # Explicit ready derived test fixture, not runtime processing.
                        derived_id=uuid4();derived_body=b'synthetic-derived-m4a'
                        derived=Asset(derived_id,'owner-a',self.artist,track,f'artists/{self.artist}/tracks/{track}/{derived_id}/audio.m4a',
                                      'audio_delivery','audio/mp4',len(derived_body),datetime.now(timezone.utc)+timedelta(minutes=5),
                                      storage_provider='local',state='ready',access_class='free',publish_state='published')
                        with self.connect() as c:
                            fields=tuple(Asset.__dataclass_fields__)
                            c.execute('INSERT INTO shadify_media_assets ('+','.join(fields)+') VALUES ('+','.join(['%s']*len(fields))+')',tuple(getattr(derived,f) for f in fields))
                            c.execute("UPDATE shadify_tracks SET delivery_asset_id=%s,publish_state='published' WHERE id=%s",(derived.id,track))
                            c.execute("UPDATE shadify_artists SET publish_state='published' WHERE id=%s",(self.artist,))
                        local=storage.adapter(derived)
                        with local.stage(derived.key,derived.mime) as staged:
                            os.write(staged[0],derived_body);local.install(staged)
                        guest_path='/media/v1/tracks/'+str(track)+'/access'
                        issued=client.get(guest_path)
                        self.assertEqual(issued.status_code,200,issued.text)
                        guest_url=issued.json()['url']
                        self.assertEqual(client.get(path(guest_url),headers={'Range':'bytes=0-3'}).content,derived_body[:4])
                        self.assertEqual(client.head(path(guest_url)).headers['content-length'],str(len(derived_body)))
                        self.assertEqual(client.get(guest_path+'?preview=wrong').status_code,422)
                        with self.connect() as c:c.execute("UPDATE shadify_tracks SET access_class='paid' WHERE id=%s",(track,))
                        self.assertEqual(client.get(guest_path).status_code,403)
                        self.assertEqual(client.get(path(guest_url)).status_code,403)
                        self.assertEqual(client.post(path(access.url)).status_code,405)
                        repo.change_owner(admitted_actor('admin'),self.artist,action='transfer',target='owner-b',expected='owner-a')
                        self.assertEqual(client.get(path(access.url)).status_code,404)
                    log.flush()
                    self.assertNotIn('token=',(root/'uvicorn.log').read_text())
                    self.assertNotIn('token=',(root/'nginx-error.log').read_text())
                finally:
                    if proxy is not None:
                        proxy.terminate();proxy.wait(timeout=5)
                    api.terminate();api.wait(timeout=5)
                    with self.connect() as c:c.execute('ALTER ROLE shadify_test RESET search_path')

    def wait_socket(self,path,process,log):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if path.exists():return
            if process.poll() is not None:raise AssertionError(log.read_text() if log.exists() else 'Test process exited')
            time.sleep(0.05)
        raise AssertionError('Disposable Unix socket startup timed out')
