"""Existing disposable Unix-socket PostgreSQL only; no external DSN or real rows."""
from pathlib import Path
from psycopg import sql
from test_artist_postgres import ArtistPostgresCase
from shadify_api.repository import PostgresMediaRepository

class ReconciledMigrationTests(ArtistPostgresCase):
 def test_selected_profile_chain_remains_compatible(self):
  root=Path(__file__).resolve().parents[1]
  self.apply(*(p.name for p in sorted((root/'migrations').glob('*.sql')) if p.name[:3]>'006' and p.name[:3]!='016'))
  self.assertEqual(PostgresMediaRepository(self.connect).public_artist_catalog(20,None,'')['artists'],[])

 def test_complete_chain_preserves_video_and_public_empty_catalog(self):
  root=Path(__file__).resolve().parents[1]
  self.apply(*(p.name for p in sorted((root/'migrations').glob('*.sql')) if p.name[:3]>'006'))
  with self.connect() as c:
   expression=c.execute("SELECT pg_get_expr(conbin,conrelid) FROM pg_constraint WHERE conrelid='shadify_ingest_variants'::regclass AND conname='shadify_profile_variants_v2'").fetchone()[0]
   for role in ('master','high','low','cover','video','cover_small','cover_avif','cover_avif_small'):
    self.assertTrue(c.execute(sql.SQL('SELECT {} FROM (SELECT %s::text AS role) r').format(sql.SQL(expression)),(role,)).fetchone()[0])
   self.assertFalse(c.execute(sql.SQL('SELECT {} FROM (SELECT %s::text AS role) r').format(sql.SQL(expression)),('unapproved',)).fetchone()[0])
  repo=PostgresMediaRepository(self.connect)
  self.assertEqual(repo.public_artist_catalog(20,None,'')['artists'],[])
