"""App-owned admin authority with local ownership and identity serialization.

Only the canonical admission adapter calls observe_identity. HTTP callers cannot
write provenance or tombstones; only the primary can change app grants through
explicit authority operations. Observation is not a central account directory
or a promise of immediate central account deletion sync.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4
from psycopg.rows import dict_row
from .media import MediaError
from .auth import Identity, require_fresh_actor
from .admin import AppAdminRepository, PERMISSIONS, PAGE_PERMISSIONS, OWNER_PERMISSIONS, active_identity


@dataclass(frozen=True)
class Authority:
    account_id: str
    user_id: object
    artist_id: object
    permissions: frozenset
    owner: bool
    publish_state: str
    acting_admin: bool = False

    @property
    def admin(self):
        return self.acting_admin

    def allows(self, permission):
        return permission in self.permissions or (self.owner and permission in OWNER_PERMISSIONS)

    def dto(self):
        return {"actor_account_id": self.account_id, "artist_id": str(self.artist_id),
                "acting_as_admin": self.admin,
                "capabilities": {"edit_profile": self.allows('edit_profile'),
                    "manage_content": self.allows('manage_content'),
                    "publish_content": self.allows('publish_content') and self.publish_state != "suspended",
                    "change_owner": self.allows('change_owner'), "suspend": self.allows('suspend'),
                    "verify": self.allows('verify')}}


class ArtistAccessRepository(AppAdminRepository):
    def observe_identity(self, identity: Identity):
        require_fresh_actor(identity)
        # Called only after require_account's complete canonical admission check.
        # An already inactive local projection stays inactive.
        with self.connect() as c:
            c.execute("INSERT INTO shadify_users (id,auth_account_id,first_admitted_at,last_admitted_at,admission_source) "
                      "VALUES (%s,%s,clock_timestamp(),clock_timestamp(),'canonical_shell') "
                      "ON CONFLICT (auth_account_id) DO UPDATE SET "
                      "first_admitted_at=COALESCE(shadify_users.first_admitted_at,EXCLUDED.first_admitted_at), "
                      "last_admitted_at=EXCLUDED.last_admitted_at,admission_source='canonical_shell'",
                      (uuid4(), identity.account_id))
            require_fresh_actor(identity)

    def _authority(self, c, actor_context: Identity, artist_id=None, *, targets=(), admin_only=False, operation=None):
        actor_context = require_fresh_actor(actor_context)
        account_id = actor_context.account_id
        # Lock artist -> app admin state -> sorted identities -> resources.
        # Grant changes and tombstones serialize against operation commit.
        artist = None
        if artist_id is not None:
            artist = c.execute("SELECT * FROM shadify_artists WHERE id=%s FOR UPDATE", (artist_id,)).fetchone()
        state = self._admin_state(c)
        identities = c.execute("SELECT * FROM shadify_users WHERE auth_account_id=ANY(%s) "
                               "ORDER BY auth_account_id FOR UPDATE", (sorted(set((account_id, *targets))),)).fetchall()
        users = {u["auth_account_id"]: u for u in identities}
        actor = users.get(account_id)
        if not active_identity(actor):
            raise MediaError(403 if admin_only else 404, "Management access unavailable")
        require_fresh_actor(actor_context)
        permissions = frozenset()
        if state['primary_user_id'] == actor['id']:
            permissions = PERMISSIONS
        elif state['primary_user_id'] is not None:
            grant = c.execute('SELECT permissions FROM shadify_app_admin_delegates WHERE user_id=%s', (actor['id'],)).fetchone()
            if grant:
                permissions = frozenset(grant['permissions'])
        # An omitted operation with admin_only is primary-only. Production
        # operations specify their capability; a delegate's subset is exact.
        if admin_only and operation is None and state['primary_user_id'] != actor['id']:
            raise MediaError(403, 'Primary application admin required')
        if operation == 'read_content':
            admin_access = bool(permissions & {'manage_content', 'publish_content'})
        elif operation is not None:
            admin_access = operation in permissions
        else:
            admin_access = bool(permissions & PAGE_PERMISSIONS) if artist_id is not None else bool(permissions)
        if admin_only and not admin_access:
            raise MediaError(403, 'Application permission required')
        if artist_id is not None and artist is None:
            raise MediaError(404, 'Artist not found')
        owner = False
        if artist is not None:
            owner = c.execute('SELECT 1 FROM artist_memberships WHERE artist_id=%s AND user_id=%s '
                              'AND revoked_at IS NULL', (artist_id, actor['id'])).fetchone() is not None
            owner_access = owner and (operation in OWNER_PERMISSIONS or operation in (None, 'read_content'))
            if not admin_access and not owner_access:
                raise MediaError(404, 'Artist not found')
        for target in targets:
            user = users.get(target)
            if not active_identity(user):
                raise MediaError(422, "Target must be an existing active canonically observed identity")
        require_fresh_actor(actor_context)
        return Authority(account_id, actor['id'], artist_id, permissions, owner,
                         artist['publish_state'] if artist else 'draft', admin_access), artist, users

    @contextmanager
    def authorized(self, actor: Identity, artist_id=None, *, targets=(), admin_only=False, operation=None, audit=True):
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                authority, artist, users = self._authority(c, actor, artist_id,
                                                         targets=targets, admin_only=admin_only, operation=operation)
                yield c, authority, artist, users
                # Runs even when the caller returns inside the context. Expiry
                # raises before psycopg commits and rolls the operation back.
                require_fresh_actor(actor)
                if audit and operation and authority.admin and operation not in ('read_content', 'read_profile', 'read_audit'):
                    self._admin_audit(c, authority, operation, artist=artist_id)
                require_fresh_actor(actor)

    def change_owner(self, actor, artist_id, *, action, target=None, expected=None):
        if action not in {"assign", "transfer", "revoke"}:
            raise ValueError("Invalid ownership action")
        if action != "revoke" and target is None:
            raise MediaError(422, "Target identity required")
        with self.authorized(actor, artist_id, targets=(() if target is None else (target,)), admin_only=True, operation='change_owner') as (c, auth, artist, users):
            owner = c.execute("SELECT m.id,u.auth_account_id FROM artist_memberships m "
                              "JOIN shadify_users u ON u.id=m.user_id WHERE m.artist_id=%s "
                              "AND m.revoked_at IS NULL", (artist_id,)).fetchone()
            current = owner["auth_account_id"] if owner else None
            if ((action == "assign" and current is not None)
                    or (action != "assign" and (current is None or current != expected))
                    or (action == "transfer" and current == target)):
                raise MediaError(409, "Owner changed or operation ineligible")
            if owner:
                c.execute("UPDATE artist_memberships SET revoked_at=clock_timestamp() WHERE id=%s", (owner["id"],))
            if action != "revoke":
                c.execute("INSERT INTO artist_memberships(id,artist_id,user_id) VALUES (%s,%s,%s)",
                          (uuid4(), artist_id, users[target]["id"]))
            c.execute("INSERT INTO artist_ownership_audit "
                      "(id,artist_id,actor_user_id,actor_account_id,action,old_owner_account_id,new_owner_account_id) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (uuid4(), artist_id, auth.user_id, actor.account_id, action, current, target if action != "revoke" else None))
            return {**auth.dto(), "owner_account_id": target if action != "revoke" else None}
