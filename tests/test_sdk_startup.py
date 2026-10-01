"""Real installed SDK/framework exercised offline; never real storage proof."""
import asyncio
import io
import socket
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import psycopg
import uvicorn
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from uvicorn.lifespan.on import LifespanOn

from shadify_api.config import StorageConfig
from shadify_api.main import app
from shadify_api.repository import PostgresMediaRepository
from shadify_api.storage import R2Storage


def test_config():
    # Explicit, synthetic unit-test values, never the user's configuration.
    return StorageConfig(
        endpoint_url="https://" + "a" * 32 + ".r2.cloudflarestorage.com",
        bucket="unit-test-private", access_key_id="UNIT_TEST_ONLY_ACCESS_KEY",
        secret_access_key="UNIT_TEST_ONLY_SECRET_KEY",
    )


class RealSDKTests(unittest.TestCase):
    def test_real_sigv4_model_and_signing_without_http(self):
        with patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("HTTP forbidden")), \
             patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")):
            storage = R2Storage(test_config())
            grant = storage.authorize_upload("originals/test/generated-id/master", "audio/flac", 20)
            url = urlsplit(grant.url)
            params = parse_qs(url.query)
            self.assertEqual(params["X-Amz-Algorithm"], ["AWS4-HMAC-SHA256"])
            self.assertEqual(params["X-Amz-Expires"], ["300"])
            signed_headers = set(params["X-Amz-SignedHeaders"][0].split(";"))
            self.assertTrue({"host", "content-length", "content-type", "if-none-match"} <= signed_headers)
            self.assertEqual(url.path, "/unit-test-private/originals/test/generated-id/master")
            self.assertEqual(grant.headers, {"Content-Type": "audio/flac", "If-None-Match": "*"})

    def test_real_sdk_head_and_range_models_with_stubbed_transport(self):
        with patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("HTTP forbidden")):
            storage = R2Storage(test_config())
            head_params = {"Bucket": "unit-test-private", "Key": "originals/test/master"}
            with Stubber(storage.client) as stub:
                stub.add_response("head_object", {"ContentLength": 20, "ContentType": "audio/flac"}, head_params)
                body = io.BytesIO(b"fLaC" + b"0" * 16)
                stub.add_response("get_object", {"Body": body}, head_params | {"Range": "bytes=0-4095"})
                info = storage.inspect("originals/test/master")
                self.assertEqual(info.size, 20)
                self.assertEqual(info.content_type, "audio/flac")
                self.assertEqual(info.prefix[:4], b"fLaC")
                self.assertTrue(body.closed)
                stub.assert_no_pending_responses()


class NoDataStartupTests(unittest.TestCase):
    def test_uvicorn_import_and_lifespan_startup_shutdown_without_listener(self):
        async def verify():
            configuration = uvicorn.Config("shadify_api.main:app", lifespan="on", log_level="warning")
            configuration.load()
            lifespan = LifespanOn(configuration)
            await lifespan.startup()
            self.assertFalse(lifespan.startup_failed)
            self.assertFalse(lifespan.should_exit)
            await lifespan.shutdown()
            self.assertFalse(lifespan.shutdown_failed)

        with patch.object(psycopg, "connect", side_effect=AssertionError("DB connection forbidden")), \
             patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("HTTP forbidden")), \
             patch.object(socket.socket, "bind", side_effect=AssertionError("Listener forbidden")), \
             patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")):
            asyncio.run(verify())

    def test_asgi_health_and_forged_identity_stay_closed_without_data_dependencies(self):
        with patch.object(psycopg, "connect", side_effect=AssertionError("DB connection forbidden")), \
             patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("HTTP forbidden")):
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/health/ready").status_code, 503)
                forged = {"X-Authenticated-Account-Id": "attacker", "X-Auth-Assertion": "forged"}
                response = client.post("/api/media/uploads", json={
                    "artist_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "track_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    "asset_type": "audio_original", "mime": "audio/flac", "size": 20,
                }, headers=forged)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertIsNotNone(PostgresMediaRepository)


if __name__ == "__main__":
    unittest.main()
