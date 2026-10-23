"""Synthetic public aliases; mounted inside an app, no listeners or live data."""
import unittest
from types import SimpleNamespace as NS
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shadify_api.public import create_public_router, PREFIX

class PublicBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.artist=uuid4();self.asset=NS(id=uuid4(),artist_id=self.artist,mime="image/png",size=12)
        def callback(name,value):
            def run(*args,**kwargs):self.events.append((name,args,kwargs));return value
            return run
        self.repo=NS(public_artist_catalog=callback("catalog",{"artists":[]}),public_page=callback("page",{}),
            public_profile_asset=callback("profile",self.asset),public_cover_asset=callback("cover",self.asset))
        access=NS(url="https://media.example/approved",expires_at="2030-01-01T00:00:00Z")
        storage=NS(profile_access=callback("profile_access",access),cover_access=callback("cover_access",access))
        app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
        app.include_router(create_public_router(repository=self.repo,storage=storage,editorial=NS(public=callback("editorial",{}))))
        self.client=TestClient(app);self.addCleanup(self.client.close)
    def test_public_routes_delegate_visibility_and_pin_assets(self):
        for suffix in ["/artists",f"/artists/{self.artist}","/artists/valid-artist",
            f"/artists/{self.artist}/images/avatar?format=avif&size=small",
            f"/tracks/{self.artist}/cover",f"/releases/{self.artist}/cover","/discover/editorial"]:
            with self.subTest(suffix=suffix):
                r=self.client.get(PREFIX+suffix,headers={"Authorization":"Bearer forged","Cookie":"fake=secret","X-Auth-Assertion":"forged"})
                self.assertEqual(r.status_code,200);self.assertEqual(r.headers["cache-control"],"no-store")
        self.assertNotIn("forged",repr(self.events));self.assertNotIn("secret",repr(self.events))
        for name,args,kwargs in self.events:
            if name.endswith("access"):self.assertEqual(kwargs["expected_asset"],self.asset.id)
    def test_invalid_queries_do_not_reach_repository(self):
        for query in ["?unknown=x","?limit=2&limit=3","?limit=21","?cursor=x","?q="+"x"*121]:
            self.assertEqual(self.client.get(PREFIX+"/artists"+query).status_code,422)
        self.assertEqual(self.events,[])
    def test_mutations_private_paths_and_bodies_are_absent(self):
        for method in ["POST","PUT","PATCH","DELETE","HEAD"]:
            self.assertEqual(self.client.request(method,PREFIX+"/artists").status_code,405)
        for path in ["/api/admin/authority",PREFIX+"/admin/authority",PREFIX+"/me","/openapi.json"]:
            self.assertEqual(self.client.get(path).status_code,404)
        self.assertEqual(self.client.request("GET",PREFIX+"/artists",content=b"forbidden").status_code,422)
        self.assertEqual(self.events,[])
    def test_bad_resources_reject(self):
        for suffix in ["/artists/INVALID","/artists/ab","/artists/x/images/master","/tracks/not-uuid/cover","/discover/editorial?q=x"]:
            self.assertEqual(self.client.get(PREFIX+suffix).status_code,422)
        self.assertEqual(self.events,[])
    def test_uncomposed_and_oversized_fail_closed(self):
        app=FastAPI();app.include_router(create_public_router())
        with TestClient(app) as client:self.assertEqual(client.get(PREFIX+"/artists").status_code,503)
        self.repo.public_artist_catalog=lambda *args:{"value":"x"*(8*1024*1024)}
        self.assertEqual(self.client.get(PREFIX+"/artists").status_code,503)
if __name__=="__main__":unittest.main()
