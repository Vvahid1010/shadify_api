"""Composition/security tests use synthetic files and admitted-scope fixtures only."""
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from app_security_shell.transport import TrustedPeer

from shadify_api.auth import identity_from_admitted_authentication
from shadify_api.config import DatabaseConfig, load_database_config
from shadify_api.domain import DomainService
from shadify_api.main import create_app
from shadify_api.runtime import create_runtime_app
from shadify_api.security import receiver_catalog


def identity_request(*, admitted=True, changes=None, extra_headers=None):
    now=int(time.time())
    claims={"purpose":"auth-assertion","account_id":"owner","session_id":"session",
            "auth_level":"mfa","domain_app":"shadify","proxy_app":"shadify_api","iat":now,"exp":now+60}
    claims.update(changes or {})
    encoded=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    # The fixture checks claim binding, not the token HMAC or native shell crypto.
    token="unit."+encoded+".synthetic"
    headers=[(b"x-auth-assertion",token.encode()),(b"x-authenticated-account-id",b"owner"),
             (b"x-authenticated-session-id",b"session"),(b"x-authenticated-auth-level",b"mfa"),
             (b"x-authenticated-domain-app",b"shadify"),(b"x-authenticated-proxy-app",b"shadify_api")]
    headers.extend(extra_headers or [])
    scope={"type":"http","method":"GET","path":"/api/me","raw_path":b"/api/me","headers":headers,"query_string":b""}
    if admitted:scope["app_security.admitted"]=TrustedPeer("fixture-connection","fixture-node","fixture-instance")
    return Request(scope)


class IdentityTests(unittest.TestCase):
    def test_identity_projection_only_after_native_admission(self):
        with self.assertRaises(HTTPException) as error:
            identity_from_admitted_authentication(identity_request(admitted=False))
        self.assertEqual(error.exception.status_code,503)
        identity=identity_from_admitted_authentication(identity_request())
        self.assertEqual(identity.account_id,"owner")
        self.assertEqual(identity.session_id,"session")

    def test_wrong_audience_domain_owner_and_expiry_rejected(self):
        for changes in ({"proxy_app":"mizfood_api"},{"domain_app":"mizfood_com"},
                        {"account_id":"attacker"},{"exp":int(time.time())-1},{"iat":True},
                        {"purpose":"oauth"},{"session_id":"other"}):
            with self.subTest(changes=changes),self.assertRaises(HTTPException) as error:
                identity_from_admitted_authentication(identity_request(changes=changes))
            self.assertEqual(error.exception.status_code,401)

    def test_duplicate_identity_headers_rejected(self):
        with self.assertRaises(HTTPException) as error:
            identity_from_admitted_authentication(identity_request(extra_headers=[(b"x-authenticated-account-id",b"owner")]))
        self.assertEqual(error.exception.status_code,401)


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.repository=Mock()
        self.artist=uuid4()
        self.track=uuid4()
        self.repository.project_user.return_value={"user_id":str(uuid4()),"account_id":"owner","artist_ids":[]}
        self.repository.create_track.return_value=self.track
        self.repository.list_tracks.return_value=[]
        self.client=TestClient(create_app(identity=lambda:"owner",domain=DomainService(self.repository)))

    def test_profile_does_not_grant_artist_capability(self):
        response=self.client.get("/api/me")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["artist_ids"],[])
        self.assertEqual(response.headers["cache-control"],"no-store")
        self.repository.project_user.assert_called_once_with("owner")

    def test_owned_draft_track_creation_and_extra_owner_rejected(self):
        response=self.client.post(f"/api/artists/{self.artist}/tracks",json={"title":"  Demo  "})
        self.assertEqual(response.status_code,201)
        self.assertEqual(response.json()["publish_state"],"draft")
        self.repository.create_track.assert_called_once_with("owner",self.artist,"Demo")
        for payload in ({"title":"Demo","owner_id":"attacker"},{"title":"   "},{"title":"a\nb"}):
            self.assertEqual(self.client.post(f"/api/artists/{self.artist}/tracks",json=payload).status_code,422)

    def test_nonowner_cannot_create_or_read_track_drafts(self):
        self.repository.create_track.return_value=None
        self.repository.list_tracks.return_value=None
        self.assertEqual(self.client.post(f"/api/artists/{self.artist}/tracks",json={"title":"Demo"}).status_code,403)
        self.assertEqual(self.client.get(f"/api/artists/{self.artist}/tracks").status_code,403)

    def test_database_failures_return_redacted_unavailable(self):
        self.repository.project_user.side_effect=RuntimeError("SYNTHETIC_SECRET_MUST_NOT_LEAK")
        response=self.client.get("/api/me")
        self.assertEqual(response.status_code,503)
        self.assertNotIn("SYNTHETIC_SECRET",response.text)


class DatabaseAndRuntimeTests(unittest.TestCase):
    def test_explicit_database_assignment_no_ambient_or_cross_app_defaults(self):
        config=DatabaseConfig(host="db.internal",dbname="assigned-shadify",user="assigned-owner",password="synthetic")
        with patch("psycopg.connect") as connect:
            config.connect()
            self.assertEqual(connect.call_args.kwargs["dbname"],"assigned-shadify")
            self.assertEqual(connect.call_args.kwargs["sslmode"],"verify-full")
        with self.assertRaises(ValueError):
            DatabaseConfig(host="db.internal",dbname="test",user="test",password="synthetic",sslmode="disable")

    def test_protected_database_file_no_secret_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/"database.credentials.json"
            path.write_text(json.dumps({"host":"127.0.0.1","dbname":"shadify-test","user":"shadify-test",
                                        "password":"SYNTHETIC_SECRET","sslmode":"disable"}))
            path.chmod(0o600)
            self.assertNotIn("SYNTHETIC_SECRET",repr(load_database_config(path)))
            path.chmod(0o644)
            with self.assertRaises(ValueError):load_database_config(path)

    def test_no_cloud_or_database_side_effect_in_configured_startup_and_closed_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            values={"shadify_api.config.json":{"app_id":"shadify_api",
                        "app_security":{"local":{"app_id":"shadify_api"}},
                        "app_security_binding":{}},
                    "app_profile.json":{"schema_version":1,"app":"shadify_api","components":{}},
                    "database.credentials.json":{"host":"127.0.0.1","dbname":"shadify-test",
                        "user":"shadify-test","password":"synthetic","sslmode":"disable"}}
            for name,data in values.items():
                path=directory/name;path.write_text(json.dumps(data));path.chmod(0o600)
            with patch("psycopg.connect",side_effect=AssertionError("DB prohibited")) as connect, \
                 patch("botocore.httpsession.URLLib3Session.send",side_effect=AssertionError("R2 prohibited")) as send:
                app=create_runtime_app(directory)
                with TestClient(app) as client:
                    self.assertEqual(client.get("/health").status_code,200)
                    self.assertEqual(client.post("/api/media/uploads",headers={"X-Authenticated-Account-Id":"owner"},json={}).status_code,503)
                connect.assert_not_called();send.assert_not_called()

    def test_missing_db_and_r2_keep_liveness_and_pending_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            values={"shadify_api.config.json":{"app_security":{"local":{"app_id":"shadify_api"}},"app_security_binding":{}},
                    "app_profile.json":{"schema_version":1,"app":"shadify_api","components":{}}}
            for name,value in values.items():
                path=directory/name;path.write_text(json.dumps(value));path.chmod(0o600)
            app=create_runtime_app(directory)
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").status_code,200)
                response=client.get("/health/ready")
                self.assertEqual(response.status_code,503)
                self.assertEqual(response.json()["checks"]["database"],"missing")
                self.assertEqual(response.json()["checks"]["storage"],"missing")

    def test_wrong_app_profile_or_shell_identity_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            for name,data in {"shadify_api.config.json":{"app_security":{},"app_security_binding":{}},
                              "app_profile.json":{"schema_version":1,"app":"mizfood_api"}}.items():
                path=directory/name;path.write_text(json.dumps(data));path.chmod(0o600)
            with self.assertRaises(RuntimeError) as error:create_runtime_app(directory)
            self.assertNotIn("mizfood",str(error.exception))

    def test_catalog_matches_management_and_authentication_only(self):
        management=json.loads((Path(__file__).resolve().parents[1]/"management_schema.json").read_text())
        self.assertEqual(receiver_catalog(),management["app_security_contract"]["receiver_catalog"])
        for endpoint in receiver_catalog():
            self.assertEqual(endpoint["permitted_peer_apps"],["authentication"])
            self.assertEqual(endpoint["receiver_app"],"shadify_api")


if __name__=="__main__":unittest.main()
