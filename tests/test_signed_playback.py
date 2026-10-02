"""Synthetic policy fixtures and real SDK GET/PUT signing; no real R2 access."""
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import socket
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit
from uuid import uuid4
from pydantic import ValidationError
from shadify_api.delivery import PlaybackService,PlaybackTarget
from shadify_api.media import Asset,MediaError
from shadify_api.storage import PlaybackAccess,R2Storage,StorageUnavailable
from test_sdk_startup import test_config


class SignedGETTests(unittest.TestCase):
    def test_real_sdk_get_ttl_opaque_access_and_upload_unchanged_without_network(self):
        with patch("botocore.httpsession.URLLib3Session.send",side_effect=AssertionError("HTTP prohibited")), \
             patch.object(socket.socket,"connect",side_effect=AssertionError("network prohibited")):
            config=test_config().model_copy(update={"playback_url_ttl_seconds":123})
            storage=R2Storage(config)
            key="delivery/artists/artist/tracks/track/asset/audio.m4a"
            access=storage.create_playback_access(key)
            query=parse_qs(urlsplit(access.url).query)
            self.assertEqual(query["X-Amz-Expires"],["123"])
            self.assertEqual(query["X-Amz-SignedHeaders"],["host"])
            self.assertEqual(query["X-Amz-Algorithm"],["AWS4-HMAC-SHA256"])
            self.assertEqual(urlsplit(access.url).path,"/unit-test-private/"+key)
            signed_at=datetime.strptime(query["X-Amz-Date"][0],"%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            self.assertLessEqual(access.expires_at,signed_at+timedelta(seconds=123))
            self.assertNotIn(access.url,repr(access))
            upload=storage.authorize_upload("originals/asset/master","audio/flac",20)
            self.assertEqual(parse_qs(urlsplit(upload.url).query)["X-Amz-Expires"],["300"])
            self.assertIn("If-None-Match",upload.headers)

    def test_get_method_expiry_and_redacted_failure(self):
        client=Mock();client.generate_presigned_url.return_value="https://unit-test.invalid/opaque"
        storage=R2Storage(test_config(),client)
        access=storage.create_playback_access("delivery/server-selected/audio.m4a")
        self.assertEqual(client.generate_presigned_url.call_args.args,("get_object",))
        self.assertEqual(client.generate_presigned_url.call_args.kwargs,
                         dict(Params={"Bucket":"unit-test-private","Key":"delivery/server-selected/audio.m4a"},ExpiresIn=900,HttpMethod="GET"))
        self.assertEqual(access.expires_at.tzinfo,timezone.utc)
        client.generate_presigned_url.side_effect=RuntimeError("SYNTHETIC_SENSITIVE_URL")
        with self.assertRaises(StorageUnavailable) as error:storage.create_playback_access("delivery/test")
        self.assertNotIn("SYNTHETIC",str(error.exception));self.assertIsNone(error.exception.__cause__)

    def test_optional_config_compatibility_and_ttl_bounds(self):
        config=test_config()
        self.assertEqual(config.playback_url_ttl_seconds,900)
        for ttl in (0,604801,True,"900",1.5):
            with self.assertRaises(ValidationError):type(config).model_validate(config.model_dump()|{"playback_url_ttl_seconds":ttl})
        for ttl in (1,604800):
            self.assertEqual(type(config).model_validate(config.model_dump()|{"playback_url_ttl_seconds":ttl}).playback_url_ttl_seconds,ttl)


class PlaybackBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.track=uuid4()
        self.asset=Asset(uuid4(),"synthetic-owner",uuid4(),self.track,"delivery/server-selected/audio.m4a",
                         "audio_delivery","audio/mp4",20,datetime.now(timezone.utc),state="ready",access_class="free",publish_state="published")
        self.target=PlaybackTarget(self.track,"published","free",self.asset)
        self.repo=Mock();self.repo.get_playback_target.return_value=self.target
        self.provider=Mock();self.provider.create_playback_access.return_value=PlaybackAccess("https://unit-test.invalid/opaque",datetime.now(timezone.utc))
        self.service=PlaybackService(self.repo,self.provider)

    def test_published_free_guest_needs_no_account_and_every_play_requests_access(self):
        first=self.service.create_playback_access(self.track)
        self.service.create_playback_access(self.track)
        self.assertEqual(self.provider.create_playback_access.call_count,2)
        self.provider.create_playback_access.assert_called_with(self.asset.key)
        self.assertEqual(first.url,"https://unit-test.invalid/opaque")
        self.assertFalse(hasattr(first,"key"))

    def test_paid_full_denied_even_with_account_before_signer(self):
        self.repo.get_playback_target.return_value=replace(self.target,track_access_class="paid")
        for account in (None,"signed-in-synthetic-owner"):
            with self.assertRaises(MediaError) as error:self.service.create_playback_access(self.track,account_id=account)
            self.assertEqual(error.exception.status,403)
        self.provider.create_playback_access.assert_not_called()

    def test_paid_preview_requires_separate_free_ready_asset(self):
        preview=replace(self.asset,id=uuid4(),key="previews/server-selected/audio.m4a",asset_type="audio_preview")
        self.assertNotEqual(preview.id,self.asset.id)
        self.repo.get_playback_target.return_value=replace(self.target,track_access_class="paid",asset=preview)
        self.service.create_playback_access(self.track,preview=True)
        self.provider.create_playback_access.assert_called_once_with(preview.key)
        self.provider.reset_mock()
        self.repo.get_playback_target.return_value=replace(self.target,track_access_class="paid")
        with self.assertRaises(MediaError):self.service.create_playback_access(self.track,preview=True)
        self.provider.create_playback_access.assert_not_called()

    def test_draft_original_unknown_and_mismatched_asset_denied_before_signer(self):
        cases=[replace(self.target,track_publish_state="draft"),replace(self.target,track_id=uuid4()),
               replace(self.target,track_access_class="private")]
        for changes in ({"state":"uploaded"},{"publish_state":"draft"},{"asset_type":"audio_original"},
                        {"key":"originals/source/master"},{"track_id":uuid4()},{"access_class":"private"},{"storage_provider":"unknown"}):
            cases.append(replace(self.target,asset=replace(self.asset,**changes)))
        for target in [None,*cases]:
            self.repo.get_playback_target.return_value=target
            with self.assertRaises(MediaError):self.service.create_playback_access(self.track)
        self.provider.create_playback_access.assert_not_called()

if __name__=="__main__":unittest.main()
