import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
from shadify_api.config import StorageConfig, load_storage_config
from shadify_api.main import create_app
from shadify_api.media import MediaError, MediaService, UploadIntent, valid_signature
from shadify_api.storage import ObjectInfo, R2Storage, StorageUnavailable, UploadAuthorization


def config_values():
    return dict(endpoint_url="https://" + "a"*32 + ".r2.cloudflarestorage.com",
                bucket="unit-test-private", access_key_id="unit-test-key",
                secret_access_key="unit-test-secret")


class Repository:
    def __init__(self):
        self.assets = {}
        self.allowed = True

    def owns_track(self, owner, artist, track):
        return self.allowed and owner == "owner"

    def insert(self, asset):
        self.assets[asset.id] = asset

    def get(self, asset_id):
        return self.assets.get(asset_id)

    def transition(self, asset_id, expected, target):
        asset = self.assets[asset_id]
        if asset.state != expected:
            return False
        self.assets[asset_id] = replace(asset, state=target)
        return True


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository()
        self.storage = Mock()
        self.storage.authorize_upload.return_value = UploadAuthorization(
            "https://unit-test.invalid/upload", {"Content-Type": "audio/flac"}, 300)
        self.storage.inspect.return_value = ObjectInfo(20, "audio/flac", b"fLaC" + b"0"*16)
        self.service = MediaService(self.repo, self.storage, 100, 300)
        self.intent = UploadIntent(artist_id=uuid4(), track_id=uuid4(),
                                  asset_type="audio_original", mime="audio/flac", size=20)

    def test_default_closed_even_with_forged_headers(self):
        client = TestClient(create_app())
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/health/ready").status_code, 503)
        response = client.post("/api/media/uploads", json=self.intent.model_dump(mode="json"),
                               headers={"X-Authenticated-Account-Id": "owner"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_upload_and_complete_stays_private_draft(self):
        client = TestClient(create_app(self.service, lambda: "owner"))
        created = client.post("/api/media/uploads", json=self.intent.model_dump(mode="json"))
        self.assertEqual(created.status_code, 201)
        self.assertNotIn("key", created.json())
        self.assertEqual(created.headers["cache-control"], "no-store")
        asset_id = created.json()["asset_id"]
        completed = client.post(f"/api/media/uploads/{asset_id}/complete")
        self.assertEqual(completed.json()["state"], "uploaded")
        self.assertEqual(completed.json()["access_class"], "private")
        self.assertEqual(completed.json()["publish_state"], "draft")
        self.assertEqual(client.post(f"/api/media/uploads/{asset_id}/complete").status_code, 200)
        self.storage.inspect.assert_called_once()

    def test_cross_owner_rejected_before_storage(self):
        with self.assertRaises(MediaError) as error:
            self.service.create("attacker", self.intent)
        self.assertEqual(error.exception.status, 403)
        self.storage.authorize_upload.assert_not_called()

    def test_completion_ownership_and_revocation(self):
        asset, _ = self.service.create("owner", self.intent)
        with self.assertRaises(MediaError) as error:
            self.service.complete("attacker", asset.id)
        self.assertEqual(error.exception.status, 404)
        self.repo.allowed = False
        with self.assertRaises(MediaError) as error:
            self.service.complete("owner", asset.id)
        self.assertEqual(error.exception.status, 403)
        self.storage.inspect.assert_not_called()

    def test_expired_session_and_new_immutable_key(self):
        first, _ = self.service.create("owner", self.intent)
        second, _ = self.service.create("owner", self.intent)
        self.assertNotEqual(first.key, second.key)
        self.repo.assets[first.id] = replace(first, expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
        with self.assertRaises(MediaError) as error:
            self.service.complete("owner", first.id)
        self.assertEqual(error.exception.status, 409)
        self.storage.inspect.assert_not_called()

    def test_type_size_and_extra_browser_keys(self):
        for changes in ({"size": 101}, {"mime": "image/png"}):
            with self.assertRaises(MediaError):
                self.service.create("owner", self.intent.model_copy(update=changes))
        for changes in ({"key": "originals/other"}, {"owner_id": "owner"}, {"size": 0}):
            with self.assertRaises(ValidationError):
                UploadIntent.model_validate(self.intent.model_dump() | changes)
        self.storage.authorize_upload.assert_not_called()

    def test_reject_bad_object_metadata_and_signature(self):
        for info in (ObjectInfo(21, "audio/flac", b"fLaC"),
                     ObjectInfo(20, "image/png", b"fLaC"),
                     ObjectInfo(20, "audio/flac", b"garbage")):
            asset, _ = self.service.create("owner", self.intent)
            self.storage.inspect.return_value = info
            with self.assertRaises(MediaError) as error:
                self.service.complete("owner", asset.id)
            self.assertEqual(error.exception.status, 422)
            self.assertEqual(self.repo.get(asset.id).state, "failed")

    def test_missing_cloud_object_does_not_claim_uploaded(self):
        asset, _ = self.service.create("owner", self.intent)
        self.storage.inspect.side_effect = StorageUnavailable()
        with self.assertRaises(StorageUnavailable):
            self.service.complete("owner", asset.id)
        self.assertEqual(self.repo.get(asset.id).state, "upload_pending")

    def test_conditional_transition_conflict(self):
        asset, _ = self.service.create("owner", self.intent)
        self.repo.transition = Mock(return_value=False)
        with self.assertRaises(MediaError) as error:
            self.service.complete("owner", asset.id)
        self.assertEqual(error.exception.status, 409)

    def test_supported_container_signatures(self):
        for mime, prefix in [("audio/wav", b"RIFF0000WAVE"), ("audio/flac", b"fLaC"),
                             ("image/png", b"\x89PNG\r\n\x1a\n"), ("image/jpeg", b"\xff\xd8\xff")]:
            self.assertTrue(valid_signature(mime, prefix))
            self.assertFalse(valid_signature(mime, b"bad"))


class AdapterTests(unittest.TestCase):
    def test_signed_params_and_bounded_read(self):
        client = Mock()
        client.generate_presigned_url.return_value = "https://unit-test.invalid/signed"
        config = StorageConfig(**config_values())
        storage = R2Storage(config, client)
        auth = storage.authorize_upload("originals/test/master", "audio/flac", 20)
        params = client.generate_presigned_url.call_args.kwargs
        self.assertEqual(params["Params"]["IfNoneMatch"], "*")
        self.assertEqual(params["Params"]["ContentLength"], 20)
        self.assertEqual(params["ExpiresIn"], 300)
        self.assertEqual(auth.headers["If-None-Match"], "*")
        self.assertNotIn(auth.url, repr(auth))
        client.head_object.return_value = {"ContentLength": 20, "ContentType": "audio/flac"}
        body = Mock()
        body.read.return_value = b"fLaC"
        client.get_object.return_value = {"Body": body}
        self.assertEqual(storage.inspect("originals/test/master").size, 20)
        self.assertEqual(client.get_object.call_args.kwargs["Range"], "bytes=0-4095")
        body.read.assert_called_once_with(4096)
        body.close.assert_called_once()

    def test_provider_failure_redacted(self):
        client = Mock()
        client.head_object.side_effect = RuntimeError("secret-signed-url")
        with self.assertRaises(StorageUnavailable) as error:
            R2Storage(StorageConfig(**config_values()), client).inspect("key")
        self.assertNotIn("secret", str(error.exception))
        self.assertIsNone(error.exception.__cause__)


class ConfigTests(unittest.TestCase):
    def test_protected_file_and_secret_repr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"storage.json"
            path.write_text(json.dumps(config_values()))
            path.chmod(0o600)
            config = load_storage_config(path)
            self.assertNotIn("unit-test-secret", repr(config))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_storage_config(path)
            path.chmod(0o600)
            link = Path(directory)/"link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                load_storage_config(link)

    def test_endpoint_ttl_placeholder_and_redacted_errors(self):
        for changes in ({"endpoint_url": "http://localhost:9000"}, {"upload_ttl_seconds": 901},
                        {"secret_access_key": "PLACEHOLDER"}, {"max_upload_bytes": 0}):
            with self.assertRaises(ValidationError):
                StorageConfig(**(config_values() | changes))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"storage.json"
            path.write_text(json.dumps(config_values() | {"secret_access_key": "secret", "wrong": "secret"}))
            path.chmod(0o600)
            with self.assertRaises(ValueError) as error:
                load_storage_config(path)
            self.assertNotIn("unit-test-key", str(error.exception))


class PostgreSQLBoundaryTests(unittest.TestCase):
    def test_parameters_and_transactional_state_change(self):
        from shadify_api.repository import PostgresMediaRepository
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.execute.return_value.fetchone.return_value = (1,)
        connection.execute.return_value.rowcount = 1
        repository = PostgresMediaRepository(lambda: connection)
        artist, track = uuid4(), uuid4()
        self.assertTrue(repository.owns_track("owner'; DROP TABLE x;--", artist, track))
        query, params = connection.execute.call_args.args
        self.assertNotIn("DROP TABLE", query)
        self.assertEqual(params, (track, artist, "owner'; DROP TABLE x;--"))
        from shadify_api.media import Asset
        asset = Asset(uuid4(), "owner", artist, track, "originals/generated/master",
                      "audio_original", "audio/flac", 20, datetime.now(timezone.utc))
        repository.insert(asset)
        query, params = connection.execute.call_args.args
        self.assertEqual(query.count("%s"), len(params))
        self.assertEqual(params[9], "r2")
        self.assertTrue(repository.transition(asset.id, "upload_pending", "uploaded"))
        query, params = connection.execute.call_args.args
        self.assertIn("AND state=%s", query)
        self.assertEqual(params, ("uploaded", asset.id, "upload_pending"))
        self.assertEqual(connection.__exit__.call_count, 3)


if __name__ == "__main__":
    unittest.main()
