from pathlib import Path
"""016 after historical 013/014 compatibility in disposable SQL only; codec/scanner fixtures are synthetic."""
import unittest
from test_ingest_pipeline import PipelineTests

class VideoMigrationCompatibility(PipelineTests):
 def setUp(self):
  super().setUp()
  with self.connect() as c:
   for name in ('013_release_suggestions.sql','014_artist_profile_media.sql'):
    c.execute((Path(__file__).parent/'fixtures'/name).read_text())
  self.apply('016_ingest_video_after_profile.sql')

def load_tests(loader,tests,pattern):
 return unittest.TestSuite(VideoMigrationCompatibility(name) for name in (
  'test_audio_and_cover_have_complete_atomic_approvals_and_immutable_masters',
  'test_worker_role_requires_no_artist_admin_write_or_identity_access',
  'test_expired_processing_attempt_cannot_commit_outputs_or_approval'))
