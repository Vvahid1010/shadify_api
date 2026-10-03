"""Real isolated SQL lifecycle/concurrency proof; synthetic identities, no R2."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import unittest
from uuid import uuid4
import psycopg
from psycopg import sql
from shadify_api.access import ArtistAccessRepository
from shadify_api.media import MediaError
from isolated_postgres import IsolatedPostgres

ROOT = Path(__file__).resolve().parents[1]


class AccessRepository(ArtistAccessRepository):
    def __init__(self, connect):
        self.connect = connect


class ArtistPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cluster = IsolatedPostgres()
        cls.server = cls.cluster.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.cluster.__exit__(None, None, None)

    def setUp(self):
        self.schema = 'test_' + uuid4().hex
        with psycopg.connect(**self.server.connection, autocommit=True) as c:
            c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.connect = lambda: psycopg.connect(**self.server.connection, options='-c search_path=' + self.schema)
        self.apply('001_media_foundation.sql', '002_user_profile_track_drafts.sql', '003_identity_provenance.sql',
                   '004_artist_memberships.sql')
        self.repo = AccessRepository(self.connect)
        # Direct synthetic test setup, not a claim of actual Authentication admission.
        for account in ('admin', 'owner-a', 'owner-b', 'listener'):
            self.repo.observe_identity(SimpleNamespace(account_id=account))
        with self.connect() as c:
            c.execute("INSERT INTO shadify_admin_grants(user_id,granted_by) "
                      "SELECT id,'isolated-test-fixture' FROM shadify_users WHERE auth_account_id='admin'")
            self.artist, self.other = uuid4(), uuid4()
            for artist in (self.artist, self.other):
                c.execute("INSERT INTO shadify_artists(id,slug,name) VALUES (%s,%s,'Test')", (artist, 'artist-' + str(artist)))

    def tearDown(self):
        with psycopg.connect(**self.server.connection, autocommit=True) as c:
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def apply(self, *names):
        with self.connect() as c:
            for name in names:
                c.execute((ROOT / 'migrations' / name).read_text())

    def assign(self, artist=None, target='owner-a'):
        return self.repo.change_owner('admin', artist or self.artist, action='assign', target=target)

    def test_unassigned_multiple_pages_and_no_identity_creation(self):
        self.assign()
        self.assign(self.other)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM artist_memberships WHERE revoked_at IS NULL').fetchone()[0], 2)
            count = c.execute('SELECT count(*) FROM shadify_users').fetchone()[0]
        for target in ('unknown', 'unproven'):
            if target == 'unproven':
                with self.connect() as c:
                    c.execute('INSERT INTO shadify_users(id,auth_account_id) VALUES (%s,%s)', (uuid4(), target))
            with self.assertRaises(MediaError) as error:
                self.repo.change_owner('admin', self.artist, action='transfer', target=target, expected='owner-a')
            self.assertEqual(error.exception.status, 422)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_users').fetchone()[0], count + 1)

    def test_transfer_revokes_old_owner_preserves_page_and_audit(self):
        self.assign()
        with self.connect() as c:
            c.execute("UPDATE shadify_artists SET publish_state='suspended',verified=true WHERE id=%s", (self.artist,))
        self.repo.change_owner('admin', self.artist, action='transfer', target='owner-b', expected='owner-a')
        with self.assertRaises(MediaError) as error:
            with self.repo.authorized('owner-a', self.artist):
                pass
        self.assertEqual(error.exception.status, 404)
        with self.repo.authorized('owner-b', self.artist) as (_, authority, artist, _):
            self.assertTrue(artist['verified'])
            self.assertEqual(artist['publish_state'], 'suspended')
            self.assertFalse(authority.dto()['capabilities']['publish_content'])
        with self.connect() as c:
            audit = c.execute('SELECT actor_account_id,old_owner_account_id,new_owner_account_id FROM artist_ownership_audit ORDER BY occurred_at,id').fetchall()
            self.assertEqual(len(audit), 2)
            self.assertIn(('admin', 'owner-a', 'owner-b'), audit)
            with self.assertRaises(psycopg.Error):
                c.execute("UPDATE artist_ownership_audit SET actor_account_id='forged'")

    def test_concurrent_assignment_and_database_unique_owner(self):
        def attempt(target):
            try:
                self.repo.change_owner('admin', self.artist, action='assign', target=target)
                return 201
            except MediaError as e:
                return e.status
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(attempt, ('owner-a', 'owner-b')))
        self.assertEqual(sorted(outcomes), [201, 409])
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM artist_memberships WHERE revoked_at IS NULL').fetchone()[0], 1)
            with self.assertRaises(psycopg.errors.UniqueViolation):
                c.execute("INSERT INTO artist_memberships(id,artist_id,user_id) SELECT %s,%s,id FROM shadify_users "
                          "WHERE auth_account_id='listener'", (uuid4(), self.artist))

    def test_stale_transfer_and_audit_failure_roll_back(self):
        self.assign()
        with self.assertRaises(MediaError) as error:
            self.repo.change_owner('admin', self.artist, action='transfer', target='owner-b', expected='wrong')
        self.assertEqual(error.exception.status, 409)
        with self.connect() as c:
            c.execute("CREATE FUNCTION reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                      "BEGIN RAISE EXCEPTION 'synthetic audit failure'; END $$")
            c.execute('CREATE TRIGGER reject_audit BEFORE INSERT ON artist_ownership_audit FOR EACH ROW EXECUTE FUNCTION reject_audit()')
        with self.assertRaises(psycopg.Error):
            self.repo.change_owner('admin', self.artist, action='transfer', target='owner-b', expected='owner-a')
        with self.repo.authorized('owner-a', self.artist):
            pass
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM artist_ownership_audit').fetchone()[0], 1)

    def test_actor_target_and_grant_revocation_serializes_until_commit(self):
        changes = [
            ("UPDATE shadify_users SET active=false WHERE auth_account_id='admin'", 'actor'),
            ("UPDATE shadify_users SET active=false WHERE auth_account_id='listener'", 'target'),
            ('UPDATE shadify_admin_grants SET revoked_at=CURRENT_TIMESTAMP', 'grant'),
        ]
        for query, kind in changes:
            with self.subTest(kind=kind):
                begun, finished = Event(), Event()
                def revoke():
                    with self.connect() as c:
                        begun.set()
                        c.execute(query)
                    finished.set()
                with ThreadPoolExecutor(1) as pool:
                    with self.repo.authorized('admin', self.artist, targets=('listener',), admin_only=True):
                        future = pool.submit(revoke)
                        self.assertTrue(begun.wait(2))
                        self.assertFalse(finished.wait(.15), 'revocation must wait for transaction commit')
                    future.result(timeout=5)
                    self.assertTrue(finished.is_set())
                with self.assertRaises(MediaError):
                    with self.repo.authorized('admin', self.artist, targets=('listener',), admin_only=True):
                        pass
                with self.connect() as c:
                    c.execute('UPDATE shadify_users SET active=true')
                    c.execute('UPDATE shadify_admin_grants SET revoked_at=NULL')

    def test_tombstone_not_reactivated_and_no_content_cascade(self):
        self.assign()
        with self.connect() as c:
            c.execute("UPDATE shadify_users SET active=false WHERE auth_account_id='owner-a'")
            c.execute('INSERT INTO shadify_tracks(id,artist_id,title) VALUES (%s,%s,%s)', (uuid4(), self.artist, 'Preserve'))
        self.repo.observe_identity(SimpleNamespace(account_id='owner-a'))
        with self.assertRaises(MediaError):
            with self.repo.authorized('owner-a', self.artist):
                pass
        self.repo.change_owner('admin', self.artist, action='revoke', expected='owner-a')
        with self.connect() as c:
            self.assertFalse(c.execute("SELECT active FROM shadify_users WHERE auth_account_id='owner-a'").fetchone()[0])
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_tracks').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_artists').fetchone()[0], 2)
            with self.assertRaises(psycopg.IntegrityError):
                c.execute("DELETE FROM shadify_users WHERE auth_account_id='owner-a'")

    def test_cross_page_and_listener_denied(self):
        self.assign()
        self.assign(self.other, 'owner-b')
        for actor in ('owner-b', 'listener'):
            with self.assertRaises(MediaError) as error:
                with self.repo.authorized(actor, self.artist):
                    pass
            self.assertEqual(error.exception.status, 404)
        with self.assertRaises(MediaError) as error:
            self.repo.change_owner('owner-a', self.artist, action='transfer', target='owner-b', expected='owner-a')
        self.assertEqual(error.exception.status, 403)

    def test_legacy_projection_without_proof_aborts_cutover(self):
        legacy_schema = 'legacy_' + uuid4().hex
        with psycopg.connect(**self.server.connection, autocommit=True) as c:
            c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(legacy_schema)))
        connect = lambda: psycopg.connect(**self.server.connection, options='-c search_path=' + legacy_schema)
        try:
            with connect() as c:
                for name in ('001_media_foundation.sql', '002_user_profile_track_drafts.sql', '003_identity_provenance.sql'):
                    c.execute((ROOT/'migrations'/name).read_text())
                artist, user = uuid4(), uuid4()
                c.execute("INSERT INTO shadify_users(id,auth_account_id) VALUES (%s,'legacy-owner')", (user,))
                c.execute("INSERT INTO shadify_artists(id,owner_id) VALUES (%s,'legacy-owner')", (artist,))
            with self.assertRaises(psycopg.Error):
                with connect() as c:
                    c.execute((ROOT/'migrations/004_artist_memberships.sql').read_text())
            with connect() as c:
                self.assertEqual(c.execute('SELECT owner_id FROM shadify_artists').fetchone()[0], 'legacy-owner')
                self.assertIsNone(c.execute("SELECT to_regclass('artist_memberships')").fetchone()[0])
            AccessRepository(connect).observe_identity(SimpleNamespace(account_id='legacy-owner'))
            with connect() as c:
                c.execute((ROOT/'migrations/004_artist_memberships.sql').read_text())
                self.assertEqual(c.execute('SELECT artist_id,user_id FROM artist_memberships').fetchone(), (artist, user))
        finally:
            with psycopg.connect(**self.server.connection, autocommit=True) as c:
                c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(legacy_schema)))
