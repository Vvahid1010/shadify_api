"""Artist-scoped managers; existing app-wide admin grants remain independent.

Compose this mixin into ArtistAccessRepository and merge its per-artist permissions
inside the existing locked _authority transaction. No bootstrap or implicit grant.
"""
from psycopg.rows import dict_row
from .auth import require_fresh_actor
from .admin import PAGE_PERMISSIONS
from .media import MediaError

MANAGER_PERMISSIONS = frozenset({'edit_profile', 'manage_content', 'publish_content'})
DEFAULT_MANAGER_PERMISSIONS = ('edit_profile', 'manage_content')


def manager_permissions(value):
    if (type(value) is not list or any(type(p) is not str for p in value)
            or len(value) != len(set(value)) or not set(value) <= MANAGER_PERMISSIONS):
        raise MediaError(422, 'Explicit artist manager permissions required')
    return sorted(value)


class ArtistManagersRepository:
    def _artist_manager_schema(self, c):
        # Optional additive feature: missing migration never creates authority.
        return c.execute("SELECT to_regclass('artist_manager_grants') IS NOT NULL AS available").fetchone()['available']

    def _artist_manager_permissions(self, c, user_id, artist_id):
        if artist_id is None or not self._artist_manager_schema(c):
            return frozenset()
        row = c.execute('SELECT permissions FROM artist_manager_grants WHERE artist_id=%s AND user_id=%s',
                        (artist_id, user_id)).fetchone()
        return frozenset(row['permissions']) if row else frozenset()

    def artist_managers(self, actor, artist_id):
        with self.authorized(actor, artist_id, admin_only=True, audit=False) as (c, auth, _, _):
            if not self._artist_manager_schema(c):
                raise MediaError(503, 'Artist manager migration unavailable')
            rows = c.execute('SELECT u.auth_account_id,u.active,g.permissions FROM artist_manager_grants g '
                             'JOIN shadify_users u ON u.id=g.user_id WHERE g.artist_id=%s '
                             'ORDER BY u.auth_account_id', (artist_id,)).fetchall()
            revision = c.execute('SELECT revision FROM shadify_app_admin WHERE singleton=true').fetchone()['revision']
            return {'actor_account_id': auth.account_id, 'artist_id': str(artist_id),
                    'revision': revision, 'managers': rows}

    def set_artist_manager(self, actor, artist_id, target, permissions, *, expected_revision):
        permissions = manager_permissions(permissions)
        if type(expected_revision) is not int or not 0 <= expected_revision <= 9223372036854775807:
            raise MediaError(422, 'Explicit authority revision required')
        require_fresh_actor(actor)
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                # Match existing artist -> app state -> sorted identities order.
                artist = c.execute('SELECT id FROM shadify_artists WHERE id=%s FOR UPDATE', (artist_id,)).fetchone()
                state = self._admin_state(c, exclusive=True)
                auth, users = self._primary_actor(c, actor, state, (target,), validate_targets=bool(permissions))
                if artist is None:
                    raise MediaError(404, 'Artist not found')
                if state['revision'] != expected_revision:
                    raise MediaError(409, 'Admin authority changed; refresh and review again')
                if not self._artist_manager_schema(c):
                    raise MediaError(503, 'Artist manager migration unavailable')
                user = users.get(target)
                if user is None:
                    raise MediaError(422, 'Existing target identity required')
                if user['id'] == state['primary_user_id']:
                    raise MediaError(409, 'Primary already oversees every artist')
                old = c.execute('SELECT permissions FROM artist_manager_grants WHERE artist_id=%s AND user_id=%s',
                                (artist_id, user['id'])).fetchone()
                if permissions:
                    c.execute('INSERT INTO artist_manager_grants(artist_id,user_id,permissions) VALUES (%s,%s,%s) '
                              'ON CONFLICT (artist_id,user_id) DO UPDATE SET permissions=EXCLUDED.permissions',
                              (artist_id, user['id'], permissions))
                else:
                    c.execute('DELETE FROM artist_manager_grants WHERE artist_id=%s AND user_id=%s', (artist_id, user['id']))
                revision = c.execute('UPDATE shadify_app_admin SET revision=revision+1 WHERE singleton=true '
                                     'AND revision=%s RETURNING revision', (expected_revision,)).fetchone()
                if revision is None:
                    raise MediaError(409, 'Admin authority changed; refresh and review again')
                self._admin_audit(c, auth, 'set_artist_manager', target=target, artist=artist_id,
                                  details={'before': old['permissions'] if old else [], 'after': permissions,
                                           'expected_revision': expected_revision, 'revision': revision['revision']})
                require_fresh_actor(actor)
                return {'actor_account_id': actor.account_id, 'artist_id': str(artist_id),
                        'target_account_id': target, 'permissions': permissions, 'revision': revision['revision']}

    def accessible_artist_pages(self, actor, limit, cursor=None, *, search=''):
        if type(search) is not str or len(search) > 120:
            raise MediaError(422, 'Artist search must be at most 120 characters')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MediaError(422, 'Invalid page limit')
        search = search.strip()
        from dataclasses import replace
        from .artists import AVAILABILITY
        with self.authorized(actor) as (c, auth, _, _):
            owned = {r['artist_id'] for r in c.execute('SELECT artist_id FROM artist_memberships '
                     'WHERE user_id=%s AND revoked_at IS NULL', (auth.user_id,)).fetchall()}
            scoped = {}
            configured = c.execute('SELECT primary_user_id FROM shadify_app_admin WHERE singleton=true').fetchone()['primary_user_id'] is not None
            if configured and self._artist_manager_schema(c):
                scoped = {r['artist_id']: frozenset(r['permissions']) for r in c.execute(
                    'SELECT artist_id,permissions FROM artist_manager_grants WHERE user_id=%s', (auth.user_id,)).fetchall()}
            global_pages = bool(auth.permissions & PAGE_PERMISSIONS)
            ids = list(owned | scoped.keys())
            rows = c.execute('SELECT a.* FROM shadify_artists a WHERE (%s OR a.id=ANY(%s::uuid[])) '
                             'AND (%s::uuid IS NULL OR a.id>%s) '
                             'AND (%s=\'\' OR position(lower(%s) in lower(a.name))>0 '
                             'OR position(lower(%s) in lower(a.slug))>0) ORDER BY a.id LIMIT %s',
                             (global_pages, ids, cursor, cursor, search, search, search, limit+1)).fetchall()
            items = []
            for row in rows[:limit]:
                permissions = auth.permissions | scoped.get(row['id'], frozenset())
                projected = replace(auth, artist_id=row['id'], publish_state=row['publish_state'],
                                    owner=row['id'] in owned, permissions=permissions,
                                    acting_admin=global_pages or row['id'] in scoped)
                items.append({'page': {k: self._page(row)[k] for k in ('id','name','slug','publish_state','verified')},
                              **projected.dto(), 'is_owner': row['id'] in owned,
                              'is_artist_manager': row['id'] in scoped})
            manager_context = bool(auth.permissions) or bool(scoped)
            return {'actor_account_id': auth.account_id, 'acting_as_admin': manager_context,
                    'is_primary': auth.primary,
                    'landing_mode': 'list' if manager_context or len(owned) != 1 else 'single_owner',
                    'sole_owned_artist_id': str(next(iter(owned))) if len(owned) == 1 else None,
                    'capabilities': {'create_artist': 'create_artist' in auth.permissions,
                                     'manage_artist_managers': auth.primary},
                    'pages': items, 'next_cursor': str(rows[limit-1]['id']) if len(rows)>limit else None,
                    'availability': AVAILABILITY}
