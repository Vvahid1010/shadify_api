"""Real disposable PostgreSQL privacy/authority tests; synthetic ingest evidence only."""
import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from pydantic import ValidationError
from psycopg.types.json import Jsonb
from authority_fixture import admitted_actor
from test_artist_postgres import ArtistPostgresCase
from shadify_api.artist_models import Track
from shadify_api.media import Asset, MediaError
from shadify_api.repository import PostgresMediaRepository
from shadify_api.mood_discovery import discover_moods


class MoodValidationTests(unittest.TestCase):
    def test_optional_controlled_distinct_labels_separate_from_genre(self):
        self.assertEqual(Track(title='Song').mood_ids, [])
        model = Track(title='Song', genre='Rock', mood_ids=['happy','calm'])
        self.assertEqual(model.genre, 'Rock')
        self.assertEqual(model.mood_ids, ['calm','happy'])
        for value in (None, 'calm', ['unknown'], ['calm','calm'], [1], ['Calm']):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Track(title='Song', mood_ids=value)


class MoodDiscoveryTests(ArtistPostgresCase):
    def setUp(self):
        super().setUp()
        self.apply('007_admin_nomination.sql','008_nomination_control.sql','009_ingest_quarantine.sql')
        self.repo = PostgresMediaRepository(self.connect)
        self.assign()
        with self.connect() as c:
            c.execute("UPDATE shadify_artists SET publish_state='published' WHERE id=%s", (self.artist,))

    def track(self, moods=None, *, artist=None, access='free'):
        artist = artist or self.artist
        result = self.repo.content_write(admitted_actor('admin'), artist, 'tracks',
            {'title':'Synthetic song','description':'Retain me','genre':'Rock',
             'access_class':access,'mood_ids':moods or []})
        return UUID(result['content']['id'])

    def asset(self, track, *, artist=None, kind='audio_delivery', access='free', approved=True):
        artist = artist or self.artist
        asset = Asset(uuid4(),'SYNTHETIC-UPLOADER',artist,track,
            ('previews/' if kind=='audio_preview' else 'delivery/') + str(uuid4()),
            kind,'audio/mp4',20,datetime.now(timezone.utc)+timedelta(minutes=5),
            storage_provider='local',state='ready',access_class=access,publish_state='published')
        with self.connect() as c:
            fields = tuple(Asset.__dataclass_fields__)
            c.execute('INSERT INTO shadify_media_assets ('+','.join(fields)+') VALUES ('+
                ','.join(['%s']*len(fields))+')', tuple(getattr(asset,field) for field in fields))
            if approved:
                # Structural gate evidence ONLY; this does not represent a real scan or output.
                source = Asset(uuid4(),'SYNTHETIC-UPLOADER',artist,track,'quarantine/'+str(uuid4()),
                    'audio_original','audio/wav',20,asset.expires_at,storage_provider='local',
                    state='uploaded',access_class='private',publish_state='draft')
                c.execute('INSERT INTO shadify_media_assets ('+','.join(fields)+') VALUES ('+
                    ','.join(['%s']*len(fields))+')', tuple(getattr(source,field) for field in fields))
                job, scan = uuid4(), uuid4()
                c.execute("INSERT INTO shadify_ingest_jobs(id,asset_id,input_sha256,state) VALUES(%s,%s,%s,'ready')",
                    (job,source.id,'a'*64))
                c.execute("INSERT INTO shadify_ingest_scans(id,job_id,input_sha256,verdict,engine_version,definitions_version,full_coverage) VALUES(%s,%s,%s,'clean','SYNTHETIC','SYNTHETIC',true)",
                    (scan,job,'a'*64))
                identity = {'device':1,'inode':1,'size':20,'mtime_ns':1,'ctime_ns':1}
                c.execute("INSERT INTO shadify_ingest_approvals(asset_id,job_id,scan_id,input_sha256,output_sha256,storage_key,storage_provider,mime,size,object_identity,validation,extraction,match_decision,processing) VALUES(%s,%s,%s,%s,%s,%s,'local',%s,%s,%s,%s,%s,%s,%s)",
                    (asset.id,job,scan,'a'*64,'b'*64,asset.key,asset.mime,asset.size,
                     Jsonb(identity),Jsonb({'passed':True}),Jsonb({'complete':True}),
                     Jsonb({'decision':'reviewed'}),Jsonb({'complete':True})))
        return asset

    def attach(self, track, asset, *, preview=None):
        with self.connect() as c:
            c.execute("UPDATE shadify_tracks SET publish_state='published',delivery_asset_id=%s,preview_asset_id=%s WHERE id=%s",
                (asset.id,preview.id if preview else None,track))

    def groups(self, **kwargs):
        return {row['mood_id']:row for row in discover_moods(self.repo, **kwargs)['playlists']}

    def test_multilabel_deterministic_order_no_private_metadata_or_unlabeled(self):
        a = self.track(['calm','romantic']); self.attach(a,self.asset(a))
        b = self.track(['calm']); self.attach(b,self.asset(b))
        untagged = self.track(); self.attach(untagged,self.asset(untagged))
        groups = self.groups()
        self.assertEqual(groups['calm']['track_ids'], sorted([str(a),str(b)]))
        self.assertEqual(groups['romantic']['track_ids'], [str(a)])
        self.assertEqual(groups['happy']['tracks'], [])
        self.assertEqual(groups['party']['tracks'], [])
        for row in groups['calm']['tracks']:
            self.assertEqual(set(row), {'id','artist_id','artist_name','title','publish_state',
                'access_class','mood_ids','delivery_asset_id'})
        self.assertNotIn(str(untagged), str(groups))

    def test_owner_edit_permission_partial_updates_preserve_metadata(self):
        track = self.track(['calm'])
        result = self.repo.content_write(admitted_actor('owner-a'),self.artist,'tracks',
            {'mood_ids':['happy','romantic']},track)['content']
        self.assertEqual(result['mood_ids'], ['romantic','happy'])
        self.assertEqual((result['title'],result['genre'],result['description']),
            ('Synthetic song','Rock','Retain me'))
        with self.assertRaises(MediaError) as error:
            self.repo.content_write(admitted_actor('listener'),self.artist,'tracks',{'mood_ids':['party']},track)
        self.assertEqual(error.exception.status,404)
        self.assertEqual(self.repo.content_read(admitted_actor('owner-a'),self.artist,'tracks',track)['content']['mood_ids'],
            ['romantic','happy'])

    def test_drafts_suspension_unapproved_assets_and_withdrawn_approval_excluded(self):
        track = self.track(['calm']); asset = self.asset(track); self.attach(track,asset)
        self.assertEqual(self.groups()['calm']['track_ids'], [str(track)])
        for table, ident, field, value, restore in (
            ('shadify_artists',self.artist,'publish_state','draft','published'),
            ('shadify_artists',self.artist,'publish_state','suspended','published'),
            ('shadify_tracks',track,'publish_state','draft','published'),
            ('shadify_media_assets',asset.id,'state','failed','ready'),
            ('shadify_media_assets',asset.id,'publish_state','draft','published'),
            ('shadify_media_assets',asset.id,'access_class','private','free')):
            with self.subTest(table=table,field=field,value=value):
                with self.connect() as c: c.execute(f'UPDATE {table} SET {field}=%s WHERE id=%s',(value,ident))
                self.assertEqual(self.groups()['calm']['tracks'], [])
                with self.connect() as c: c.execute(f'UPDATE {table} SET {field}=%s WHERE id=%s',(restore,ident))
        bad = self.track(['calm']); self.attach(bad,self.asset(bad,approved=False))
        self.assertEqual(self.groups()['calm']['track_ids'], [str(track)])
        with self.connect() as c:
            c.execute("UPDATE shadify_ingest_jobs SET state='rejected',revision=revision+1 WHERE id=(SELECT job_id FROM shadify_ingest_approvals WHERE asset_id=%s)",(asset.id,))
        self.assertEqual(self.groups()['calm']['tracks'], [])

    def test_paid_full_is_never_queue_source_and_preview_requires_approval(self):
        track = self.track(['happy'],access='paid')
        full = self.asset(track,access='paid'); self.attach(track,full)
        self.assertEqual(self.groups()['happy']['tracks'], [])
        preview = self.asset(track,kind='audio_preview',approved=False)
        self.attach(track,full,preview=preview)
        self.assertEqual(self.groups()['happy']['tracks'], [])
        preview = self.asset(track,kind='audio_preview')
        self.attach(track,full,preview=preview)
        row = self.groups()['happy']['tracks'][0]
        self.assertEqual(row['preview_asset_id'],str(preview.id))
        self.assertNotIn('delivery_asset_id',row)

    def test_pipeline_schema_requires_pipeline_evidence_and_pagination_remains_stable(self):
        tracks = [self.track(['party']) for _ in range(3)]
        for track in tracks: self.attach(track,self.asset(track))
        first = self.groups(mood_id='party',limit=1)['party']
        self.assertEqual(first['track_ids'],sorted(map(str,tracks))[:1])
        second = self.groups(mood_id='party',limit=1,cursor=UUID(first['next_cursor']))['party']
        self.assertEqual(second['track_ids'],sorted(map(str,tracks))[1:2])
        self.apply('010_ingest_attempts.sql','011_ingest_processing.sql')
        self.assertEqual(self.groups()['party']['tracks'], [])

    def test_bounded_scan_cursor_advances_over_ineligible_rows(self):
        from unittest.mock import patch
        tracks = [self.track(['calm']) for _ in range(2)]
        for track in tracks:
            self.attach(track,self.asset(track))
        low, high = sorted(tracks)
        with self.connect() as c:
            c.execute('UPDATE shadify_tracks SET delivery_asset_id=NULL WHERE id=%s',(low,))
        with patch('shadify_api.mood_discovery.CANDIDATE_LIMIT',1):
            first = self.groups(mood_id='calm')['calm']
            self.assertEqual(first['next_cursor'],str(low))
            next_page = self.groups(mood_id='calm',cursor=UUID(first['next_cursor']))['calm']
            self.assertEqual(first['tracks'], [])
            self.assertEqual(next_page['track_ids'], [str(high)])
            self.assertIsNone(next_page['next_cursor'])


    def ineligible_candidates(self, count, moods):
        # Published synthetic rows whose missing delivery prevents public membership.
        with self.connect() as c:
            with c.cursor() as query:
                query.executemany("INSERT INTO shadify_tracks(id,artist_id,title,data,publish_state) VALUES(%s,%s,'Missing delivery',%s,'published')",
                    [(UUID(int=i),self.artist,Jsonb({'mood_ids':moods})) for i in range(1,count+1)])

    def test_real_hundred_candidate_cap_keeps_empty_pages_continuable_and_later_tracks(self):
        self.ineligible_candidates(100,['calm','romantic'])
        tracks = [self.track(['calm','romantic']) for _ in range(2)]
        for track in tracks:
            self.attach(track,self.asset(track))
        initial = self.groups()
        combined = {}
        for mood in ('calm','romantic'):
            first = initial[mood]
            self.assertEqual(first['tracks'], [])
            self.assertEqual(first['next_cursor'],str(UUID(int=100)))
            second = self.groups(mood_id=mood,limit=1,cursor=UUID(first['next_cursor']))[mood]
            retry = self.groups(mood_id=mood,limit=1,cursor=UUID(first['next_cursor']))[mood]
            self.assertEqual(second,retry)
            self.assertEqual(second['track_ids'], sorted(map(str,tracks))[:1])
            self.assertEqual(second['next_cursor'], second['track_ids'][0])
            third = self.groups(mood_id=mood,limit=1,cursor=UUID(second['next_cursor']))[mood]
            self.assertEqual(third['track_ids'],sorted(map(str,tracks))[1:])
            self.assertIsNone(third['next_cursor'])
            combined[mood] = second['track_ids'] + third['track_ids']
            self.assertEqual(len(combined[mood]),len(set(combined[mood])))
        # Both labels intentionally match the same songs in separate playlists.
        # An all-moods aggregate contains each stable track identity just once.
        self.assertEqual(combined['calm'],combined['romantic'])
        self.assertEqual(set(combined['calm'] + combined['romantic']),set(map(str,tracks)))

    def test_exact_cap_and_all_ineligible_tail_have_correct_completion_cursors(self):
        self.ineligible_candidates(100,['party'])
        exact = self.groups(mood_id='party')['party']
        self.assertEqual(exact['tracks'],[])
        self.assertIsNone(exact['next_cursor'])
        with self.connect() as c:
            c.execute("INSERT INTO shadify_tracks(id,artist_id,title,data,publish_state) VALUES(%s,%s,'Missing delivery',%s,'published')",
                (UUID(int=101),self.artist,Jsonb({'mood_ids':['party']})))
        first = self.groups(mood_id='party')['party']
        self.assertEqual(first['tracks'],[])
        self.assertEqual(first['next_cursor'],str(UUID(int=100)))
        tail = self.groups(mood_id='party',cursor=UUID(first['next_cursor']))['party']
        self.assertEqual(tail['tracks'],[])
        self.assertIsNone(tail['next_cursor'])

    def test_invalid_requests_fail_without_reading_private_data(self):
        for args in ({'mood_id':'unknown'},{'limit':True},{'limit':21},{'cursor':uuid4()}):
            with self.subTest(args=args), self.assertRaises(MediaError) as error:
                discover_moods(self.repo,**args)
            self.assertEqual(error.exception.status,422)
