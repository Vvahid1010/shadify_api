"""Source ASGI + real isolated PostgreSQL; synthetic Auth and R2 only.

These tests do not prove guest managed ingress, processing or real R2 access.
"""
from datetime import datetime,timedelta,timezone
from dataclasses import replace
from uuid import UUID,uuid4
from unittest.mock import Mock
from fastapi import HTTPException
from fastapi.testclient import TestClient
from shadify_api.auth import observed_account
from shadify_api.domain import DomainService
from shadify_api.main import create_app
from shadify_api.media import Asset,MediaError,MediaService,UploadIntent
from shadify_api.repository import PostgresMediaRepository
from shadify_api.storage import ObjectInfo,UploadAuthorization
from test_artist_postgres import ArtistPostgresCase
from test_integration import identity_request


class ArtistApiTests(ArtistPostgresCase):
    def setUp(self):
        super().setUp()
        self.apply('005_artist_content.sql')
        self.repo=PostgresMediaRepository(self.connect)
        self.storage=Mock()
        self.storage.authorize_upload.return_value=UploadAuthorization('https://unit-test.invalid/put',{'If-None-Match':'*'},300)
        self.storage.inspect.return_value=ObjectInfo(20,'audio/flac',b'fLaC'+b'0'*16)
        self.media=MediaService(self.repo,self.storage,100,300)
        self.actor='admin'
        self.client=TestClient(create_app(self.media,lambda:self.actor,domain=DomainService(self.repo),repository=self.repo))

    def page_path(self,artist=None): return '/api/artists/'+str(artist or self.artist)
    def track(self,artist=None,title='Track'):
        response=self.client.post(self.page_path(artist)+'/tracks',json={'title':title})
        self.assertEqual(response.status_code,201,response.text)
        return UUID(response.json()['track_id'])
    def ready_asset(self,track,*,artist=None,kind='audio_delivery',access='free'):
        # Trusted synthetic processing output fixture; never cloud/real readiness proof.
        artist=artist or self.artist
        prefix='previews/' if kind=='audio_preview' else 'delivery/'
        asset=Asset(uuid4(),'audit-uploader',artist,track,prefix+str(uuid4())+'/asset',kind,'audio/mp4',20,
                    datetime.now(timezone.utc)+timedelta(minutes=5),state='ready',access_class=access,publish_state='published')
        with self.connect() as c:
            fields=tuple(Asset.__dataclass_fields__)
            c.execute('INSERT INTO shadify_media_assets ('+','.join(fields)+') VALUES ('+','.join(['%s']*len(fields))+')',
                      tuple(getattr(asset,f) for f in fields))
        return asset
    def published_track(self,title='Track',*,access='free'):
        track=self.track(title=title)
        asset=self.ready_asset(track,access=access)
        url=self.page_path()+'/tracks/'+str(track)
        r=self.client.patch(url,json={'delivery_asset_id':str(asset.id),'access_class':access})
        self.assertEqual(r.status_code,200,r.text)
        r=self.client.put(url+'/publication',json={'state':'published'})
        self.assertEqual(r.status_code,200,r.text)
        return track,asset
    def upload(self,track):
        return self.media.create(self.actor,UploadIntent(artist_id=self.artist,track_id=track,
                asset_type='audio_original',mime='audio/flac',size=20))[0]

    def test_admin_unassigned_curated_page_and_official_moments(self):
        response=self.client.post('/api/admin/artists',json={'slug':'curated-test','name':'Curated'})
        self.assertEqual(response.status_code,201,response.text)
        artist=UUID(response.json()['page']['id']); path=self.page_path(artist)
        self.assertTrue(response.json()['acting_as_admin'])
        draft=self.client.post(path+'/moments',json={'text':'Private draft'})
        official=self.client.post(path+'/moments',json={'text':'Official notice'})
        self.assertEqual(draft.status_code,201,draft.text)
        self.assertEqual(official.status_code,201,official.text)
        moment=official.json()['content']['id']
        self.assertEqual(self.client.put(path+'/moments/'+moment+'/publication',json={'state':'published'}).status_code,200)
        self.assertEqual(self.client.put(path+'/layout',json={'pinned_moment_id':moment,'sections':[
            {'id':'moments','position':0,'visible':True}]}).status_code,200)
        self.assertEqual(self.client.put(path+'/publication',json={'state':'published'}).status_code,200)
        self.actor='guest'
        public=self.client.get('/api/artist-slugs/curated-test/public')
        self.assertEqual(public.status_code,200,public.text)
        self.assertEqual(self.client.get('/api/artist-slugs/curated-test/public?limit=100').status_code,422)
        body=public.json()
        self.assertTrue(body['page']['curated_by_shadify'])
        self.assertEqual([m['text'] for m in body['content']['moments']],['Official notice'])
        self.assertTrue(body['content']['moments'][0]['official_artist_news'])
        for forbidden in ('owner_account_id','actor_account_id','author_account_id','originals/','signed'):
            self.assertNotIn(forbidden,public.text)
        self.assertEqual(self.client.get(path).status_code,404)
        self.assertEqual(self.client.get(path+'/moments/'+moment).status_code,404)

    def test_owner_multi_page_capabilities_and_admin_non_enumeration(self):
        self.assign(); self.assign(self.other)
        self.actor='owner-a'
        manageable=self.client.get('/api/me/artists').json()
        self.assertEqual(len(manageable['pages']),2)
        for page in manageable['pages']:
            self.assertFalse(page['acting_as_admin'])
            self.assertFalse(page['capabilities']['verify'])
        for artist in (self.artist,uuid4()):
            r=self.client.put('/api/admin/artists/'+str(artist)+'/verification',json={'verified':True})
            self.assertEqual(r.status_code,403,r.text)
        self.assertEqual(self.client.post('/api/admin/artists',json={'name':'Self','slug':'self-test'}).status_code,403)
        for patch in ({'verified':True},{'owner_id':'owner-a'},{'is_artist':True},{'publish_state':'published'},{'admin':True}):
            self.assertEqual(self.client.patch(self.page_path(),json=patch).status_code,422)

    def test_identity_assignment_proof_and_canonical_observer(self):
        with self.connect() as c:
            user=uuid4()
            c.execute("INSERT INTO shadify_users(id,auth_account_id) VALUES (%s,'owner')",(user,))
        target='/api/admin/artists/'+str(self.artist)+'/owner'
        self.assertEqual(self.client.post(target,json={'account_id':'owner'}).status_code,422)
        observer=observed_account(self.repo)
        with self.assertRaises(HTTPException): observer(identity_request(admitted=False))
        with self.connect() as c:
            self.assertIsNone(c.execute("SELECT first_admitted_at FROM shadify_users WHERE auth_account_id='owner'").fetchone()[0])
        self.assertEqual(observer(identity_request()),'owner')
        self.assertEqual(self.client.post(target,json={'account_id':'owner'}).status_code,200)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT id FROM shadify_users WHERE auth_account_id='owner'").fetchone()[0],user)
        self.assertEqual(self.client.post('/api/admin/artists/'+str(self.other)+'/owner',json={'account_id':'unknown'}).status_code,422)

    def test_transfer_http_preserves_work_slug_and_revokes_all_management(self):
        self.assign()
        self.actor='owner-a'; track=self.track()
        moment=self.client.post(self.page_path()+'/moments',json={'text':'Original owner notice'}).json()['content']['id']
        self.actor='admin'
        self.client.put('/api/admin/artists/'+str(self.artist)+'/verification',json={'verified':True})
        r=self.client.put('/api/admin/artists/'+str(self.artist)+'/owner',json={'account_id':'owner-b','expected_owner_account_id':'owner-a'})
        self.assertEqual(r.status_code,200,r.text)
        self.actor='owner-a'
        for method,path,payload in (
            ('get',self.page_path(),None),('get',self.page_path()+'/tracks',None),
            ('post',self.page_path()+'/tracks',{'title':'Denied'}),
            ('patch',self.page_path()+'/tracks/'+str(track),{'title':'Denied'}),
            ('delete',self.page_path()+'/moments/'+moment,None),
            ('put',self.page_path()+'/publication',{'state':'published'})):
            kwargs={} if payload is None else {'json':payload}
            self.assertEqual(getattr(self.client,method)(path,**kwargs).status_code,404)
        self.actor='owner-b'
        page=self.client.get(self.page_path()).json()
        self.assertTrue(page['page']['verified'])
        self.assertEqual(page['page']['slug'],'artist-'+str(self.artist))
        self.assertEqual(self.client.get(self.page_path()+'/tracks/'+str(track)).status_code,200)
        self.actor='admin'
        audit=self.client.get('/api/admin/artists/'+str(self.artist)+'/ownership-audit').json()['events']
        self.assertTrue(any(e['actor_account_id']=='admin' and e['old_owner_account_id']=='owner-a' and e['new_owner_account_id']=='owner-b' for e in audit))

    def test_cross_artist_content_release_layout_and_media_idor(self):
        self.assign(); self.assign(self.other,'owner-b')
        self.actor='owner-a'; a=self.track(); asset=self.upload(a)
        self.actor='owner-b'; b=self.track(self.other)
        path=self.page_path(self.other)
        for method,url,payload in (
            ('get',path+'/tracks/'+str(a),None),('patch',path+'/tracks/'+str(a),{'title':'No'}),
            ('delete',path+'/tracks/'+str(a),None),
            ('put',path+'/tracks/'+str(a)+'/publication',{'state':'published'}),
            ('post',path+'/releases',{'title':'No','track_ids':[str(a)]}),
            ('post',path+'/videos',{'title':'No','track_id':str(a)}),
            ('post',path+'/moments',{'text':'No','track_id':str(a)}),
            ('put',path+'/layout',{'featured_work':{'kind':'track','id':str(a)}}),
            ('patch',path+'/tracks/'+str(b),{'original_asset_id':str(asset.id)}),
            ('get','/api/media/'+str(asset.id),None),
            ('post','/api/media/uploads/'+str(asset.id)+'/complete',None),
            ('post','/api/media/uploads',{'artist_id':str(self.artist),'track_id':str(a),
                                       'asset_type':'audio_original','mime':'audio/flac','size':20})):
            kwargs={} if payload is None else {'json':payload}
            response=getattr(self.client,method)(url,**kwargs)
            self.assertEqual(response.status_code,404,(url,response.text))
        self.storage.inspect.assert_not_called()

    def test_completion_and_failure_after_transfer_recheck_and_new_owner_attachment(self):
        for bad in (False,True):
            with self.subTest(bad=bad):
                # Restore active owner between scenarios through explicit admin lifecycle.
                with self.connect() as c:
                    active=c.execute('SELECT u.auth_account_id FROM artist_memberships m JOIN shadify_users u ON u.id=m.user_id '
                                     'WHERE artist_id=%s AND revoked_at IS NULL',(self.artist,)).fetchone()
                if active:
                    self.repo.change_owner('admin',self.artist,action='transfer',target='owner-a',expected='owner-b')
                else: self.assign()
                self.actor='owner-a'; track=self.track(); asset=self.upload(track)
                def inspect(key):
                    self.repo.change_owner('admin',self.artist,action='transfer',target='owner-b',expected='owner-a')
                    return ObjectInfo(21 if bad else 20,'audio/flac',b'fLaC'+b'0'*16)
                self.storage.inspect.side_effect=inspect
                with self.assertRaises(MediaError) as error: self.media.complete('owner-a',asset.id)
                self.assertEqual(error.exception.status,404)
                self.assertEqual(self.repo.get(asset.id).state,'upload_pending')
                self.storage.inspect.side_effect=None
                completed=self.media.complete('owner-b',asset.id)
                self.assertEqual(completed.state,'uploaded')
                self.assertEqual(completed.owner_id,'owner-a')
                self.actor='owner-b'
                attached=self.client.patch(self.page_path()+'/tracks/'+str(track),json={'original_asset_id':str(asset.id)})
                self.assertEqual(attached.status_code,200,attached.text)
                with self.assertRaises(MediaError): self.media.complete('owner-a',asset.id)
                self.assertEqual(self.media.complete('owner-b',asset.id).state,'uploaded')
                metadata=self.client.get('/api/media/'+str(asset.id))
                self.assertEqual(metadata.status_code,200)
                self.assertNotIn(asset.key,metadata.text)

    def test_suspension_and_verification_independent_and_no_public_leaks(self):
        self.assign(); self.actor='owner-a'
        track,asset=self.published_track(); self.client.put(self.page_path()+'/publication',json={'state':'published'})
        self.actor='admin'
        self.client.put('/api/admin/artists/'+str(self.artist)+'/verification',json={'verified':True})
        response=self.client.put('/api/admin/artists/'+str(self.artist)+'/suspension',json={'state':'suspended'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.put(self.page_path()+'/publication',json={'state':'published'}).status_code,409)
        self.actor='owner-a'
        self.assertEqual(self.client.get(self.page_path()+'/public').status_code,404)
        self.assertEqual(self.client.put(self.page_path()+'/publication',json={'state':'published'}).status_code,409)
        self.assertEqual(self.client.put(self.page_path()+'/tracks/'+str(track)+'/publication',json={'state':'published'}).status_code,409)
        self.assertEqual(self.client.patch(self.page_path(),json={'bio':'Edit while suspended'}).status_code,200)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT publish_state FROM shadify_tracks WHERE id=%s',(track,)).fetchone()[0],'published')
            self.assertTrue(c.execute('SELECT verified FROM shadify_artists WHERE id=%s',(self.artist,)).fetchone()[0])
        self.actor='admin'; self.client.put('/api/admin/artists/'+str(self.artist)+'/suspension',json={'state':'published'})
        self.actor='guest'
        public=self.client.get(self.page_path()+'/public')
        self.assertEqual(public.status_code,200,public.text)
        self.assertEqual(public.json()['content']['tracks'][0]['id'],str(track))
        self.assertNotIn(asset.key,public.text)

    def test_release_order_readiness_and_reference_preserving_delete(self):
        first,asset=self.published_track('First'); second,other=self.published_track('Second')
        release=self.client.post(self.page_path()+'/releases',json={'title':'Album','kind':'Album','track_ids':[str(second),str(first)]})
        self.assertEqual(release.status_code,201,release.text)
        release_id=release.json()['content']['id']; url=self.page_path()+'/releases/'+release_id
        self.assertEqual(self.client.put(url+'/publication',json={'state':'published'}).status_code,200)
        self.client.put(self.page_path()+'/layout',json={'featured_work':{'kind':'release','id':release_id}})
        self.assertEqual(self.client.delete(url).status_code,409)
        self.client.put(self.page_path()+'/publication',json={'state':'published'})
        body=self.client.get(self.page_path()+'/public').json()
        self.assertEqual(body['content']['releases'][0]['track_ids'],[str(second),str(first)])
        self.assertEqual(self.client.delete(self.page_path()+'/tracks/'+str(first)).status_code,409)
        self.client.put(self.page_path()+'/tracks/'+str(first)+'/publication',json={'state':'draft'})
        body=self.client.get(self.page_path()+'/public').json()
        self.assertEqual(body['content']['releases'],[])
        self.assertIsNone(body['page']['layout']['featured_work'])
        self.client.put(self.page_path()+'/layout',json={})
        self.assertEqual(self.client.delete(url).status_code,200)

    def test_unready_video_originals_events_and_layout_validation(self):
        track=self.track(); asset=self.upload(track); self.media.complete('admin',asset.id)
        url=self.page_path()+'/tracks/'+str(track)
        self.assertEqual(self.client.patch(url,json={'original_asset_id':str(asset.id)}).status_code,200)
        self.assertEqual(self.client.put(url+'/publication',json={'state':'published'}).status_code,409)
        video=self.client.post(self.page_path()+'/videos',json={'title':'Live','kind':'live_performance','track_id':str(track)})
        self.assertEqual(video.status_code,201)
        video_id=video.json()['content']['id']
        self.assertEqual(self.client.put(self.page_path()+'/videos/'+video_id+'/publication',json={'state':'published'}).status_code,409)
        event={'title':'Show','starts_at':'2026-11-01T19:00:00+03:30','timezone':'Asia/Tehran','city':'City','venue':'Venue',
               'ticket_url':'https://example.invalid/ticket'}
        r=self.client.post(self.page_path()+'/events',json=event)
        self.assertEqual(r.status_code,201,r.text)
        self.assertEqual(r.json()['content']['starts_at'],'2026-11-01T15:30:00+00:00')
        self.assertEqual(self.client.patch(self.page_path()+'/events/'+r.json()['content']['id'],json={'cancelled':True}).status_code,200)
        for patch in ({'timezone':'Fake/Zone'},{'starts_at':'2026-11-01T19:00:00'},{'ticket_url':'javascript:bad'}):
            self.assertEqual(self.client.post(self.page_path()+'/events',json=event|patch).status_code,422)
        for body in ({'sections':[{'id':'merch','position':0,'visible':True}]},
                     {'sections':[{'id':'songs','position':0,'visible':True},{'id':'songs','position':1,'visible':False}]}):
            self.assertEqual(self.client.put(self.page_path()+'/layout',json=body).status_code,422)
        self.assertFalse(self.client.get(self.page_path()).json()['availability']['processing'])
        self.assertEqual(self.client.post(self.page_path()+'/moments',json={'text':'Mention','official_artist_news':True}).status_code,422)

    def test_admin_public_user_fields_only_and_parameterized_sql(self):
        with self.connect() as c:
            user=c.execute("SELECT id FROM shadify_users WHERE auth_account_id='listener'").fetchone()[0]
        path='/api/admin/users/'+str(user)+'/profile'
        response=self.client.patch(path,json={'name':'Listener','bio':'Public biography','slug':'listener-profile'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(set(response.json()['profile']),{'user_id','name','slug','bio','genres'})
        for field in ('password','email','active','first_admitted_at','admin'):
            self.assertEqual(self.client.patch(path,json={field:'forged'}).status_code,422)
        self.actor='listener'; self.assertEqual(self.client.get(path).status_code,403)
        self.assertFalse(self.repo.owns_track("owner'; DROP TABLE x;--",self.artist,uuid4()))
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_users').fetchone()[0],4)
        self.assertTrue(self.repo.ready())

    def test_public_optional_references_never_leak_private_music_or_asset(self):
        track=self.track()
        moment=self.client.post(self.page_path()+'/moments',json={'text':'Upcoming music','track_id':str(track)}).json()['content']['id']
        self.client.put(self.page_path()+'/moments/'+moment+'/publication',json={'state':'published'})
        video_asset=self.ready_asset(track,kind='video_delivery')
        video=self.client.post(self.page_path()+'/videos',json={'title':'Performance','track_id':str(track),
                                'delivery_asset_id':str(video_asset.id)}).json()['content']['id']
        self.assertEqual(self.client.put(self.page_path()+'/videos/'+video+'/publication',json={'state':'published'}).status_code,200)
        playable,asset=self.published_track()
        artwork=self.ready_asset(playable,kind='artwork_delivery')
        self.client.patch(self.page_path()+'/tracks/'+str(playable),json={'artwork_asset_id':str(artwork.id)})
        # Simulate a trusted future producer withdrawing a derived asset.
        with self.connect() as c:
            c.execute("UPDATE shadify_media_assets SET publish_state='draft' WHERE id=%s",(artwork.id,))
        self.client.put(self.page_path()+'/publication',json={'state':'published'})
        self.actor='guest'
        public=self.client.get(self.page_path()+'/public')
        self.assertEqual(public.status_code,200,public.text)
        body=public.json()['content']
        self.assertIsNone(body['moments'][0]['track_id'])
        self.assertIsNone(body['videos'][0]['track_id'])
        self.assertIsNone(body['tracks'][0]['artwork_asset_id'])
        self.assertNotIn(str(artwork.id),public.text)
        self.assertNotIn(str(track),public.text)
