"""Small app-owned admin boundary; no operational bootstrap HTTP endpoint."""
from uuid import uuid4
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .auth import require_fresh_actor
from .media import MediaError

PERMISSIONS = frozenset({'create_artist', 'edit_profile', 'manage_content',
    'publish_content', 'change_owner', 'suspend', 'verify', 'edit_user_profile'})
PAGE_PERMISSIONS = PERMISSIONS - {'create_artist', 'edit_user_profile'}
OWNER_PERMISSIONS = frozenset({'edit_profile', 'manage_content', 'publish_content'})


def active_identity(user):
    return (user is not None and user['active'] and user['first_admitted_at'] is not None
            and user['admission_source'] == 'canonical_shell')


class AppAdminRepository:
    def _admin_state(self, c, *, exclusive=False):
        # Always before identity/resource locks. SHARE allows parallel operations;
        # UPDATE serializes transfers/grant changes against in-flight transactions.
        state = c.execute('SELECT * FROM shadify_app_admin WHERE singleton=true FOR ' +
                          ('UPDATE' if exclusive else 'SHARE')).fetchone()
        if state is None:
            raise MediaError(503, 'Application admin schema unavailable')
        return state

    def _admin_audit(self, c, actor, action, *, target=None, artist=None, details=None, schema=None):
        audit_id = uuid4()
        relation = sql.Identifier(schema, 'shadify_app_admin_audit') if schema is not None else sql.Identifier('shadify_app_admin_audit')
        c.execute(sql.SQL('INSERT INTO {} '
                  '(id,actor_user_id,actor_account_id,target_account_id,artist_id,action,details) '
                  'VALUES (%s,%s,%s,%s,%s,%s,%s)').format(relation),
                  (audit_id, actor.user_id if actor else None, actor.account_id if actor else None,
                   target, artist, action, Jsonb(details or {})))
        return audit_id

    def bootstrap_primary(self, target, *, operator_reference):
        """Protected local preparation seam, never wired to a request or config.

        Tests call this explicitly with synthetic admitted identities. A real
        bootstrap/recovery entry point and operator authority remain unselected.
        No first-login/e-mail/legacy-grant promotion exists.
        """
        require_fresh_actor(target)
        if type(operator_reference) is not str or not operator_reference.strip() or len(operator_reference) > 200:
            raise MediaError(422, 'Protected operator reference required')
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                state = self._admin_state(c, exclusive=True)
                if state['primary_user_id'] is not None:
                    raise MediaError(409, 'Primary already configured')
                user = c.execute('SELECT * FROM shadify_users WHERE auth_account_id=%s FOR UPDATE',
                                 (target.account_id,)).fetchone()
                if not active_identity(user):
                    raise MediaError(422, 'Target must be an existing active canonically observed identity')
                c.execute('UPDATE shadify_app_admin SET primary_user_id=%s,revision=revision+1 WHERE singleton=true', (user['id'],))
                self._admin_audit(c, None, 'bootstrap_primary', target=target.account_id,
                                  details={'operator_reference': operator_reference})
                require_fresh_actor(target)

    def _primary_actor(self, c, actor, state, targets=(), *, validate_targets=True):
        require_fresh_actor(actor)
        users = c.execute('SELECT * FROM shadify_users WHERE auth_account_id=ANY(%s) '
                          'ORDER BY auth_account_id FOR UPDATE',
                          (sorted(set((actor.account_id, *targets))),)).fetchall()
        users = {u['auth_account_id']: u for u in users}
        user = users.get(actor.account_id)
        if not active_identity(user) or state['primary_user_id'] != user['id']:
            raise MediaError(403, 'Primary application admin required')
        for target in targets:
            if validate_targets and not active_identity(users.get(target)):
                raise MediaError(422, 'Target must be an existing active canonically observed identity')
        require_fresh_actor(actor)
        from .access import Authority
        return Authority(actor.account_id, user['id'], None, PERMISSIONS, False, 'draft', True, True), users

    def set_delegate(self, actor, target, permissions, *, expected_revision):
        if type(expected_revision) is not int or not 0 <= expected_revision <= 9223372036854775807:
            raise MediaError(422, 'Explicit authority revision required')
        if (type(permissions) is not list or any(type(p) is not str for p in permissions)
                or len(set(permissions)) != len(permissions) or not set(permissions) <= PERMISSIONS):
            raise MediaError(422, 'Explicit existing-operation permissions required')
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                state = self._admin_state(c, exclusive=True)
                auth, users = self._primary_actor(c, actor, state, (target,), validate_targets=bool(permissions))
                if state['revision'] != expected_revision:
                    raise MediaError(409, 'Admin authority changed; refresh and review again')
                # Revocation must remain possible for an inactive/tombstoned delegate.
                user = users.get(target)
                if user is None:
                    raise MediaError(422, 'Existing target identity required')
                if user['id'] == state['primary_user_id']:
                    raise MediaError(409, 'Primary cannot be a delegate')
                old = c.execute('SELECT permissions FROM shadify_app_admin_delegates WHERE user_id=%s', (user['id'],)).fetchone()
                if permissions:
                    c.execute('INSERT INTO shadify_app_admin_delegates(user_id,permissions) VALUES (%s,%s) '
                              'ON CONFLICT (user_id) DO UPDATE SET permissions=EXCLUDED.permissions', (user['id'], sorted(permissions)))
                else:
                    c.execute('DELETE FROM shadify_app_admin_delegates WHERE user_id=%s', (user['id'],))
                changed = c.execute('UPDATE shadify_app_admin SET revision=revision+1 '
                                    'WHERE singleton=true AND revision=%s RETURNING revision',
                                    (expected_revision,)).fetchone()
                if changed is None:
                    raise MediaError(409, 'Admin authority changed; refresh and review again')
                self._admin_audit(c, auth, 'set_delegate', target=target,
                                  details={'before': old['permissions'] if old else [], 'after': sorted(permissions),
                                           'expected_revision': expected_revision, 'revision': changed['revision']})
                require_fresh_actor(actor)
                return {'actor_account_id': actor.account_id, 'target_account_id': target,
                        'permissions': sorted(permissions), 'revision': changed['revision']}

    def transfer_primary(self, actor, target, *, expected_primary, delegates):
        """Ordinary in-app transfer is closed; no local maintenance bypass.

        VM primary assignment/recovery uses the separately protected Node
        nomination path. The WSL maintenance adapter remains owner-pending.
        """
        require_fresh_actor(actor)
        raise MediaError(403, 'Primary assignment and transfer require protected control')

    def admin_details(self, actor):
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                state = self._admin_state(c)
                auth, _ = self._primary_actor(c, actor, state)
                delegates = c.execute('SELECT u.auth_account_id,d.permissions,u.active FROM shadify_app_admin_delegates d '
                                      'JOIN shadify_users u ON u.id=d.user_id ORDER BY u.auth_account_id').fetchall()
                require_fresh_actor(actor)
                return {'primary_account_id': actor.account_id, 'revision': state['revision'], 'delegates': delegates}

    def admin_audit(self, actor, limit=20, cursor=None):
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                state = self._admin_state(c)
                self._primary_actor(c, actor, state)
                rows = c.execute('SELECT * FROM shadify_app_admin_audit WHERE (%s::uuid IS NULL OR id>%s) '
                                 'ORDER BY id LIMIT %s', (cursor, cursor, limit+1)).fetchall()
                from uuid import UUID
                events = [{k:str(v) if isinstance(v,UUID) else v for k,v in r.items()} for r in rows[:limit]]
                require_fresh_actor(actor)
                return {'events':events, 'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None}
