"""Single artist authority, serialized with ownership, identity and grant revocation.

Only the canonical admission adapter calls observe_identity. HTTP callers cannot
write provenance, tombstones or admin grants. Local observation is not a central
account directory or a promise of immediate central account deletion sync.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4
from psycopg.rows import dict_row
from .media import MediaError


@dataclass(frozen=True)
class Authority:
    account_id: str
    user_id: object
    artist_id: object
    admin: bool
    publish_state: str

    def dto(self):
        return {"actor_account_id": self.account_id, "artist_id": str(self.artist_id),
                "acting_as_admin": self.admin,
                "capabilities": {"edit_profile": True, "manage_content": True,
                    "publish_content": self.publish_state != "suspended",
                    "change_owner": self.admin, "suspend": self.admin, "verify": self.admin}}


class ArtistAccessRepository:
    def observe_identity(self, identity):
        # Called only after require_account's complete canonical admission check.
        # An already inactive local projection stays inactive.
        with self.connect() as c:
            c.execute("INSERT INTO shadify_users (id,auth_account_id,first_admitted_at,last_admitted_at,admission_source) "
                      "VALUES (%s,%s,clock_timestamp(),clock_timestamp(),'canonical_shell') "
                      "ON CONFLICT (auth_account_id) DO UPDATE SET "
                      "first_admitted_at=COALESCE(shadify_users.first_admitted_at,EXCLUDED.first_admitted_at), "
                      "last_admitted_at=EXCLUDED.last_admitted_at,admission_source='canonical_shell'",
                      (uuid4(), identity.account_id))

    def _authority(self, c, account_id, artist_id=None, *, targets=(), admin_only=False):
        # All page operations lock artist -> identities sorted by account ID -> grant.
        # Trusted identity/grant revocation UPDATE/DELETE conflicts with these row
        # locks until commit; neither needs an artist lock. Never reverse this order.
        artist = None
        if artist_id is not None:
            artist = c.execute("SELECT * FROM shadify_artists WHERE id=%s FOR UPDATE", (artist_id,)).fetchone()
        identities = c.execute("SELECT * FROM shadify_users WHERE auth_account_id=ANY(%s) "
                               "ORDER BY auth_account_id FOR UPDATE", (sorted(set((account_id, *targets))),)).fetchall()
        users = {u["auth_account_id"]: u for u in identities}
        actor = users.get(account_id)
        if (actor is None or not actor["active"] or actor["first_admitted_at"] is None
                or actor["admission_source"] != "canonical_shell"):
            raise MediaError(403 if admin_only else 404, "Management access unavailable")
        grant = c.execute("SELECT revoked_at FROM shadify_admin_grants WHERE user_id=%s FOR UPDATE",
                          (actor["id"],)).fetchone()
        admin = grant is not None and grant["revoked_at"] is None
        if admin_only and not admin:
            raise MediaError(403, "Application admin required")
        if artist_id is not None and artist is None:
            raise MediaError(404, "Artist not found")
        if artist is not None and not admin:
            owner = c.execute("SELECT 1 FROM artist_memberships WHERE artist_id=%s AND user_id=%s "
                              "AND revoked_at IS NULL", (artist_id, actor["id"])).fetchone()
            if owner is None:
                raise MediaError(404, "Artist not found")
        for target in targets:
            user = users.get(target)
            if (user is None or not user["active"] or user["first_admitted_at"] is None
                    or user["admission_source"] != "canonical_shell"):
                raise MediaError(422, "Target must be an existing active canonically observed identity")
        return Authority(account_id, actor["id"], artist_id, admin,
                         artist["publish_state"] if artist else "draft"), artist, users

    @contextmanager
    def authorized(self, account_id, artist_id=None, *, targets=(), admin_only=False):
        with self.connect() as connection:
            with connection.cursor(row_factory=dict_row) as c:
                authority, artist, users = self._authority(c, account_id, artist_id,
                                                         targets=targets, admin_only=admin_only)
                yield c, authority, artist, users

    def change_owner(self, actor, artist_id, *, action, target=None, expected=None):
        if action not in {"assign", "transfer", "revoke"}:
            raise ValueError("Invalid ownership action")
        if action != "revoke" and target is None:
            raise MediaError(422, "Target identity required")
        with self.authorized(actor, artist_id, targets=(() if target is None else (target,)), admin_only=True) as (c, auth, artist, users):
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
                      (uuid4(), artist_id, auth.user_id, actor, action, current, target if action != "revoke" else None))
            return {**auth.dto(), "owner_account_id": target if action != "revoke" else None}
