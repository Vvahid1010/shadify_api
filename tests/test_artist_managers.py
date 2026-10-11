"""Disposable SQL proof; synthetic accounts only, no operational migration/grant."""
import unittest
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
import time
from shadify_api.artist_managers import ArtistManagersRepository
from shadify_api.access import ArtistAccessRepository
from shadify_api.artists import ArtistOperations
from shadify_api.media import MediaError
from authority_fixture import admitted_actor
from test_artist_postgres import ArtistPostgresCase


ManagerAccess = ArtistAccessRepository if issubclass(ArtistAccessRepository, ArtistManagersRepository) else type(
    'ManagerAccess', (ArtistManagersRepository, ArtistAccessRepository), {})


class ManagerRepository(ManagerAccess, ArtistOperations):
    def __init__(self, connect):
        self.connect = connect


class ArtistManagerTests(ArtistPostgresCase):
    def setUp(self):
        super().setUp()
        self.apply('015_artist_managers.sql')
        self.repo = ManagerRepository(self.connect)

    def grant(self, target='listener', artist=None, permissions=None):
        return self.repo.set_artist_manager(admitted_actor('admin'), artist or self.artist, target,
            ['edit_profile', 'manage_content'] if permissions is None else permissions,
            expected_revision=self.admin_revision())

    def test_artist_scope_multiple_pages_and_default_capabilities(self):
        self.grant()
        result = self.repo.accessible_artist_pages(admitted_actor('listener'), 100)
        self.assertEqual([r['page']['id'] for r in result['pages']], [str(self.artist)])
        self.assertEqual(result['landing_mode'], 'list')
        capabilities = result['pages'][0]['capabilities']
        self.assertTrue(capabilities['edit_profile'])
        self.assertTrue(capabilities['manage_content'])
        for key in ('publish_content', 'change_owner', 'suspend', 'verify'):
            self.assertFalse(capabilities[key])
        self.grant(artist=self.other, permissions=['publish_content'])
        result = self.repo.accessible_artist_pages(admitted_actor('listener'), 100)
        self.assertEqual(len(result['pages']), 2)
        self.grant(permissions=[])
        self.assertEqual([r['page']['id'] for r in self.repo.accessible_artist_pages(admitted_actor('listener'),100)['pages']], [str(self.other)])

    def test_only_primary_grants_and_scoped_manager_cannot_elevate_or_transfer(self):
        self.grant()
        self.assign()
        for account in ('listener', 'owner-a'):
            with self.assertRaises(MediaError) as error:
                self.repo.set_artist_manager(admitted_actor(account), self.artist, 'owner-b', ['manage_content'], expected_revision=self.admin_revision())
            self.assertEqual(error.exception.status,403)
            with self.assertRaises(MediaError) as error:
                self.repo.artist_managers(admitted_actor(account),self.artist)
            self.assertEqual(error.exception.status,403)
        for permissions in (['change_owner'], ['verify'], ['suspend'], ['create_artist'], ['manage_content','manage_content'], [True]):
            with self.assertRaises(MediaError) as error: self.grant(permissions=permissions)
            self.assertEqual(error.exception.status,422)
        with self.assertRaises(MediaError) as error:
            self.repo.change_owner(admitted_actor('listener'),self.artist,action='transfer',target='owner-b',expected='owner-a')
        self.assertEqual(error.exception.status,403)

    def test_unknown_unproven_inactive_targets_never_create_identity(self):
        with self.connect() as c:
            c.execute("INSERT INTO shadify_users(id,auth_account_id) VALUES (%s,'unproven')",(uuid4(),))
            c.execute("UPDATE shadify_users SET active=false WHERE auth_account_id='owner-b'")
            before = c.execute('SELECT count(*) FROM shadify_users').fetchone()[0]
        for target in ('unknown','unproven','owner-b','person@example.invalid'):
            with self.assertRaises(MediaError) as error: self.grant(target=target)
            self.assertEqual(error.exception.status,422)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM shadify_users').fetchone()[0],before)
            self.assertEqual(c.execute('SELECT count(*) FROM artist_manager_grants').fetchone()[0],0)

    def test_inactive_target_can_be_revoked_and_denied_as_actor(self):
        self.grant()
        with self.connect() as c: c.execute("UPDATE shadify_users SET active=false WHERE auth_account_id='listener'")
        with self.assertRaises(MediaError): self.repo.accessible_artist_pages(admitted_actor('listener'),100)
        self.grant(permissions=[])
        self.assertEqual(self.repo.artist_managers(admitted_actor('admin'),self.artist)['managers'],[])

    def test_cas_conflict_has_no_grant_or_audit_effect(self):
        revision = self.admin_revision()
        self.grant()
        with self.connect() as c: before=c.execute('SELECT count(*) FROM shadify_app_admin_audit').fetchone()[0]
        with self.assertRaises(MediaError) as error:
            self.repo.set_artist_manager(admitted_actor('admin'),self.artist,'listener',[],expected_revision=revision)
        self.assertEqual(error.exception.status,409)
        self.assertEqual(len(self.repo.artist_managers(admitted_actor('admin'),self.artist)['managers']),1)
        with self.connect() as c: self.assertEqual(c.execute('SELECT count(*) FROM shadify_app_admin_audit').fetchone()[0],before)

    def test_owner_main_admin_and_global_delegate_semantics_remain_independent(self):
        self.assign()
        self.assertEqual(self.repo.accessible_artist_pages(admitted_actor('owner-a'),100)['landing_mode'],'single_owner')
        self.assign(self.other)
        self.assertEqual(self.repo.accessible_artist_pages(admitted_actor('owner-a'),100)['landing_mode'],'list')
        self.grant(target='owner-a')
        self.repo.change_owner(admitted_actor('admin'),self.artist,action='transfer',target='owner-b',expected='owner-a')
        self.assertEqual(len(self.repo.accessible_artist_pages(admitted_actor('owner-a'),100)['pages']),2)
        self.assertEqual(len(self.repo.accessible_artist_pages(admitted_actor('admin'),100)['pages']),2)
        self.repo.set_delegate(admitted_actor('admin'),'listener',['verify'],expected_revision=self.admin_revision())
        self.assertEqual(len(self.repo.accessible_artist_pages(admitted_actor('listener'),100)['pages']),2)
        self.repo.page_state(admitted_actor('listener'),self.other,None,admin_only=True,verification=True)

    def test_search_filters_only_authorized_pages_before_pagination(self):
        self.grant()
        with self.connect() as c:
            c.execute("UPDATE shadify_artists SET name='Allowed Singer' WHERE id=%s",(self.artist,))
            c.execute("UPDATE shadify_artists SET name='Private Singer' WHERE id=%s",(self.other,))
        self.assertEqual(len(self.repo.accessible_artist_pages(admitted_actor('listener'),1,search='sINGER')['pages']),1)
        self.assertEqual(self.repo.accessible_artist_pages(admitted_actor('listener'),100,search='Private')['pages'],[])
        self.assertEqual(self.repo.accessible_artist_pages(admitted_actor('listener'),100,search='%')['pages'],[])
        with self.assertRaises(MediaError): self.repo.accessible_artist_pages(admitted_actor('listener'),100,search='x'*121)

    def test_expiry_during_artist_lock_wait_prevents_grant(self):
        now=int(time.time()); clock=[now]; entered=Event()
        actor=admitted_actor('admin',issued=now,expires=now+1)
        from shadify_api.auth import require_fresh_actor
        def checked(value):
            result=require_fresh_actor(value); entered.set(); return result
        with patch('shadify_api.auth.time.time',side_effect=lambda:clock[0]), patch('shadify_api.artist_managers.require_fresh_actor',side_effect=checked):
            with ThreadPoolExecutor(1) as pool:
                with self.connect() as c:
                    c.execute('SELECT id FROM shadify_artists WHERE id=%s FOR UPDATE',(self.artist,))
                    future=pool.submit(self.repo.set_artist_manager,actor,self.artist,'listener',['manage_content'],expected_revision=self.admin_revision())
                    self.assertTrue(entered.wait(2)); clock[0]=actor.expires_at
                with self.assertRaises(MediaError) as error: future.result(timeout=5)
                self.assertEqual(error.exception.status,401)
        self.assertEqual(self.repo.artist_managers(admitted_actor('admin'),self.artist)['managers'],[])


if __name__ == '__main__': unittest.main()
