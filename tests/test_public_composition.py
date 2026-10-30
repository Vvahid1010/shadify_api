import unittest
from types import SimpleNamespace as NS
from fastapi.testclient import TestClient
from shadify_api.main import create_app
from shadify_api.public import public_receiver_catalog
from shadify_api.security import receiver_catalog

class PublicCompositionTests(unittest.TestCase):
 def test_composed_catalog_and_private_identity_boundary(self):
  repo=NS(public_artist_catalog=lambda *a:{"artists":[]})
  with TestClient(create_app(repository=repo)) as client:
   self.assertEqual(client.get('/api/public/artists').status_code,200)
   self.assertEqual(client.get('/api/me').status_code,503)
   self.assertEqual(client.get('/api/admin/authority').status_code,503)
   self.assertEqual(client.post('/api/public/artists').status_code,405)
  allrows=receiver_catalog(); rows=public_receiver_catalog()
  self.assertEqual(len(rows),6)
  self.assertEqual(len({r['endpoint_id'] for r in allrows}),len(allrows))
  for row in rows:
   self.assertIn(row,allrows)
   self.assertEqual(row['permitted_peer_apps'],['authentication'])
   self.assertEqual(row['method'],'GET')
   self.assertEqual(row['headers'],['accept','cache-control','content-type','referrer-policy','x-content-type-options'])
   self.assertFalse(row['proxy_headers'])
   self.assertEqual(row['max_response_bytes'],8388608)

 def test_legacy_handlers_and_aliases_have_identical_dtos(self):
  from uuid import uuid4
  from unittest.mock import patch
  from shadify_api.storage_service import StorageService
  from shadify_api.editorial_discovery import EditorialDiscovery
  artist=uuid4();asset=NS(id=uuid4(),artist_id=artist,mime='image/png',size=12)
  access=NS(url='https://media.example/fixture',expires_at='2030-01-01T00:00:00Z')
  repo=NS(public_artist_catalog=lambda *a:{'artists':[]},public_page=lambda **k:{'page':{'name':'fixture'}},
          public_profile_asset=lambda *a,**k:asset,public_cover_asset=lambda *a:asset)
  class FixtureStorage(StorageService):
   def __init__(self):pass
   def profile_access(self,*args,**kw):
    assert kw['expected_asset']==asset.id
    return access
   def cover_access(self,*args,**kw):
    assert kw['expected_asset']==asset.id
    return access
  pairs=[('/api/artist-catalog','/api/public/artists'),
   (f'/api/artists/{artist}/public',f'/api/public/artists/{artist}'),
   ('/api/artist-slugs/valid-artist/public','/api/public/artists/valid-artist'),
   (f'/api/artists/{artist}/profile-media/avatar/access',f'/api/public/artists/{artist}/images/avatar'),
   (f'/api/releases/{artist}/cover/access',f'/api/public/releases/{artist}/cover'),
   (f'/api/tracks/{artist}/cover/access',f'/api/public/tracks/{artist}/cover'),
   ('/api/discover/editorial','/api/public/discover/editorial')]
  with patch.object(EditorialDiscovery,'public',return_value={'revision':0,'rails':[]}):
   with TestClient(create_app(service=NS(storage=FixtureStorage()),repository=repo)) as client:
    for old,new in pairs:
     with self.subTest(path=new):
      a,b=client.get(old),client.get(new)
      self.assertEqual(a.status_code,200);self.assertEqual(b.status_code,200)
      self.assertEqual(a.json(),b.json())
 def test_canonical_catalog_accepts_alias_contract(self):
  from app_security_shell.routing import normalized_catalog
  rows=normalized_catalog(receiver_catalog())
  self.assertTrue(rows)
