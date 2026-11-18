"""Unwired app-owned nomination core. No contact parsing, tag minting or HTTP adapter.

NominationTrust is a mandatory trusted integration seam, not a browser proof or
operator reference. Its exact implementation awaits the Authentication-owned
projection/confirmation contract. These internal records are not wire schemas.
"""
import hmac
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID
from psycopg.rows import dict_row
from psycopg.errors import UniqueViolation
from .admin import AppAdminRepository, active_identity
from .auth import Identity, require_fresh_actor
from .media import MediaError


@dataclass(frozen=True)
class NominationScope:
    app_instance_id: UUID
    deployment: str
    app: str = 'shadify_api'
    domain: str = 'shadify'


@dataclass(frozen=True)
class NominationCommand:
    operation_id: UUID
    nomination_id: UUID
    scope: NominationScope
    action: str
    expected_nomination_revision: int
    expected_admin_revision: int
    mode: str
    email_tag: str
    phone_tag: str
    expires_at: int
    expected_primary_account_id: str | None = None
    delegates: str | None = None


@dataclass(frozen=True)
class VerifiedContacts:
    account_id: str
    scope: NominationScope
    email_tag: str
    phone_tag: str
    email_verified: bool
    phone_verified: bool
    expires_at: int


class NominationTrust(Protocol):
    def authorize_change(self, operator: Identity, command: NominationCommand) -> bool: ...
    def verified_contacts(self, actor: Identity, scope: NominationScope) -> VerifiedContacts: ...


@dataclass(frozen=True)
class AuditActor:
    account_id: str
    user_id: UUID | None


class NominationRepository(AppAdminRepository):
    def __init__(self, connect, scope, *, trust=None, max_ttl_seconds=None):
        # Mandatory deployment policy, no invented wire-format TTL default.
        if (type(scope) is not NominationScope or scope.app != 'shadify_api' or scope.domain != 'shadify'
                or type(scope.app_instance_id) is not UUID or type(scope.deployment) is not str
                or not 1 <= len(scope.deployment) <= 128
                or type(max_ttl_seconds) is not int or not 1 <= max_ttl_seconds <= 604800):
            raise ValueError('Explicit nomination scope and bounded deployment policy required')
        self.connect, self.scope, self.trust, self.max_ttl = connect, scope, trust, max_ttl_seconds

    def _scope(self, scope):
        if type(scope) is not NominationScope or scope != self.scope:
            raise MediaError(403, 'Nomination scope mismatch')

    def _tag(self, value):
        # Opaque trusted tags only. No email/phone normalization or hashing here.
        if type(value) is not str or not 1 <= len(value) <= 1024:
            raise MediaError(422, 'Both opaque contact selectors required')

    def _command(self, command):
        if type(command) is not NominationCommand:
            raise MediaError(422, 'Nomination command required')
        self._scope(command.scope)
        if (type(command.operation_id) is not UUID or type(command.nomination_id) is not UUID
                or command.action not in ('register','renew','replace','revoke')
                or type(command.expected_nomination_revision) is not int or command.expected_nomination_revision < 0
                or type(command.expected_admin_revision) is not int or command.expected_admin_revision < 0):
            raise MediaError(422, 'Explicit nomination operation and revisions required')
        if command.action == 'revoke': return
        self._tag(command.email_tag); self._tag(command.phone_tag)
        now = int(time.time())
        if type(command.expires_at) is not int or not now < command.expires_at <= now + self.max_ttl:
            raise MediaError(422, 'Bounded future nomination expiry required')
        if command.mode == 'initialize_primary':
            if command.expected_primary_account_id is not None or command.delegates is not None:
                raise MediaError(422, 'Initialize explicitly expects no primary or delegates policy')
        elif command.mode == 'recover_primary':
            if not command.expected_primary_account_id or command.delegates not in ('retain','revoke'):
                raise MediaError(422, 'Recovery requires expected primary and explicit delegates decision')
        else: raise MediaError(422, 'Explicit initialize or recover required')

    def _primary_account(self, c, state):
        if state['primary_user_id'] is None: return None
        row = c.execute('SELECT auth_account_id FROM shadify_users WHERE id=%s', (state['primary_user_id'],)).fetchone()
        if row is None: raise MediaError(503, 'Primary identity unavailable')
        return row['auth_account_id']

    def _expected(self, c, state, revision, primary):
        if state['revision'] != revision or self._primary_account(c, state) != primary:
            raise MediaError(409, 'App authority revision or expected primary changed')

    def _row(self, c, nomination_id):
        row = c.execute('SELECT * FROM shadify_admin_nominations WHERE id=%s FOR UPDATE', (nomination_id,)).fetchone()
        if row is None: raise MediaError(404, 'Nomination not found')
        if row['app_instance_id'] != self.scope.app_instance_id or row['deployment'] != self.scope.deployment:
            raise MediaError(403, 'Nomination scope mismatch')
        return row

    def _fresh_row(self, row, expected_revision, *, expiry=True):
        if row['state'] != 'pending' or row['revision'] != expected_revision:
            raise MediaError(409, 'Nomination state or revision changed')
        if expiry and row['expires_at'] <= datetime.now(timezone.utc):
            raise MediaError(409, 'Nomination expired')

    def _dto(self, row):
        state = 'expired' if row['state'] == 'pending' and row['expires_at'] <= datetime.now(timezone.utc) else row['state']
        return {'nomination_id': str(row['id']), 'revision': row['revision'], 'state': state,
                'mode': row['mode'], 'expires_at': row['expires_at'],
                'expected_admin_revision': row['expected_admin_revision'],
                'expected_primary_account_id': row['expected_primary_account_id'],
                'delegates': row['delegates'],
                'claimed_user_id': str(row['claimed_user_id']) if row['claimed_user_id'] else None}

    def change_nomination(self, operator, command):
        require_fresh_actor(operator); self._command(command)
        if self.trust is None: raise MediaError(503, 'Nomination trust integration pending')
        if self.trust.authorize_change(operator, command) is not True:
            raise MediaError(403, 'Scoped human confirmation required')
        require_fresh_actor(operator)
        try:
            with self.connect() as connection:
                with connection.cursor(row_factory=dict_row) as c:
                    state = self._admin_state(c, exclusive=True)
                    self._expected(c, state, command.expected_admin_revision, command.expected_primary_account_id)
                    require_fresh_actor(operator)
                    operator_row = c.execute('SELECT * FROM shadify_users WHERE auth_account_id=%s FOR UPDATE', (operator.account_id,)).fetchone()
                    if operator_row is not None and not active_identity(operator_row):
                        raise MediaError(403, 'Human operator inactive')
                    audit_actor = AuditActor(operator.account_id, operator_row['id'] if operator_row else None)
                    if command.action == 'register':
                        if command.expected_nomination_revision != 0:
                            raise MediaError(409, 'Registration expects no existing nomination revision')
                        # Explicitly terminalize expired intents; never grant them.
                        expired = c.execute("SELECT * FROM shadify_admin_nominations WHERE state='pending' FOR UPDATE").fetchone()
                        if expired:
                            if expired['app_instance_id'] != self.scope.app_instance_id or expired['deployment'] != self.scope.deployment:
                                raise MediaError(403, 'Existing pending nomination belongs to another scope')
                            if expired['expires_at'] > datetime.now(timezone.utc):
                                raise MediaError(409, 'Pending nomination already exists')
                            c.execute("UPDATE shadify_admin_nominations SET state='expired',revision=revision+1,updated_at=clock_timestamp() WHERE id=%s", (expired['id'],))
                            self._admin_audit(c, audit_actor, 'nomination_expired', details={'nomination_id': str(expired['id'])})
                        row = c.execute('INSERT INTO shadify_admin_nominations '
                                        '(id,app_instance_id,deployment,state,mode,email_tag,phone_tag,expected_admin_revision,expected_primary_account_id,delegates,expires_at) '
                                        "VALUES (%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                                        (command.nomination_id,self.scope.app_instance_id,self.scope.deployment,command.mode,
                                         command.email_tag,command.phone_tag,command.expected_admin_revision,
                                         command.expected_primary_account_id,command.delegates,datetime.fromtimestamp(command.expires_at,timezone.utc))).fetchone()
                        before = 0
                    else:
                        old = self._row(c, command.nomination_id)
                        self._fresh_row(old, command.expected_nomination_revision, expiry=False)
                        before = old['revision']
                        if command.action == 'revoke':
                            row = c.execute("UPDATE shadify_admin_nominations SET state='revoked',revision=revision+1,updated_at=clock_timestamp() WHERE id=%s RETURNING *", (old['id'],)).fetchone()
                        else:
                            if command.action == 'renew' and (old['mode'],old['email_tag'],old['phone_tag'],old['delegates']) != (command.mode,command.email_tag,command.phone_tag,command.delegates):
                                raise MediaError(409, 'Renew cannot replace the selector or operation')
                            row = c.execute('UPDATE shadify_admin_nominations SET revision=revision+1,mode=%s,email_tag=%s,phone_tag=%s,'
                                            'expected_admin_revision=%s,expected_primary_account_id=%s,delegates=%s,expires_at=%s,updated_at=clock_timestamp() '
                                            'WHERE id=%s RETURNING *', (command.mode,command.email_tag,command.phone_tag,command.expected_admin_revision,
                                            command.expected_primary_account_id,command.delegates,datetime.fromtimestamp(command.expires_at,timezone.utc),old['id'])).fetchone()
                    self._admin_audit(c, audit_actor, 'nomination_' + command.action,
                                      details={'nomination_id': str(row['id']), 'nomination_operation_id': str(command.operation_id),
                                               'before_revision': before, 'after_revision': row['revision'],
                                               'mode': row['mode'], 'scope': {'app': self.scope.app, 'domain': self.scope.domain,
                                                'app_instance_id': str(self.scope.app_instance_id), 'deployment': self.scope.deployment}})
                    require_fresh_actor(operator)
                    if command.action != 'revoke' and row['expires_at'] <= datetime.now(timezone.utc):
                        raise MediaError(409, 'Nomination expired before commit')
                    return self._dto(row)
        except UniqueViolation:
            raise MediaError(409, 'Nomination or operation already exists') from None

    def claim_nomination(self, actor, scope, nomination_id, *, expected_revision, operation_id):
        require_fresh_actor(actor); self._scope(scope)
        if type(nomination_id) is not UUID or type(operation_id) is not UUID or type(expected_revision) is not int or expected_revision <= 0:
            raise MediaError(422, 'Explicit nomination id, revision and operation required')
        if self.trust is None: raise MediaError(503, 'Nomination trust integration pending')
        proof = self.trust.verified_contacts(actor, scope)
        self._contacts(proof, actor)
        require_fresh_actor(actor)
        try:
            with self.connect() as connection:
                with connection.cursor(row_factory=dict_row) as c:
                    # Reuse app state -> nomination -> identity lock order.
                    state = self._admin_state(c, exclusive=True)
                    row = self._row(c, nomination_id)
                    self._fresh_row(row, expected_revision)
                    self._expected(c, state, row['expected_admin_revision'], row['expected_primary_account_id'])
                    users = c.execute('SELECT * FROM shadify_users WHERE auth_account_id=%s FOR UPDATE', (actor.account_id,)).fetchone()
                    if not active_identity(users): raise MediaError(422, 'Active canonically admitted target required')
                    self._contacts(proof, actor)
                    if not (hmac.compare_digest(row['email_tag'].encode(), proof.email_tag.encode())
                            and hmac.compare_digest(row['phone_tag'].encode(), proof.phone_tag.encode())):
                        raise MediaError(403, 'Both verified contacts must match the same account')
                    previous = c.execute('SELECT u.auth_account_id,d.permissions FROM shadify_app_admin_delegates d '
                                         'JOIN shadify_users u ON u.id=d.user_id ORDER BY u.auth_account_id').fetchall()
                    if row['mode'] == 'initialize_primary':
                        if previous: raise MediaError(409, 'Unconfigured app contains unexpected delegate state')
                    else:
                        if row['expected_primary_account_id'] == actor.account_id:
                            raise MediaError(409, 'Recovery target already primary')
                        if row['delegates'] == 'revoke': c.execute('DELETE FROM shadify_app_admin_delegates')
                        else: c.execute('DELETE FROM shadify_app_admin_delegates WHERE user_id=%s', (users['id'],))
                    c.execute('UPDATE shadify_app_admin SET primary_user_id=%s,revision=revision+1 WHERE singleton=true', (users['id'],))
                    updated = c.execute("UPDATE shadify_admin_nominations SET state='claimed',claimed_user_id=%s,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s RETURNING *", (users['id'],row['id'])).fetchone()
                    self._admin_audit(c, AuditActor(actor.account_id, users['id']), 'nomination_claim', target=actor.account_id,
                                      details={'nomination_id': str(row['id']), 'nomination_operation_id': str(operation_id),
                                               'mode': row['mode'], 'old_primary': row['expected_primary_account_id'],
                                               'delegates': row['delegates'], 'previous_delegates': previous,
                                               'before_revision': row['revision'], 'after_revision': updated['revision']})
                    self._contacts(proof, actor)
                    require_fresh_actor(actor)
                    if row['expires_at'] <= datetime.now(timezone.utc): raise MediaError(409, 'Nomination expired before commit')
                    return {**self._dto(updated), 'primary_account_id': actor.account_id}
        except UniqueViolation:
            raise MediaError(409, 'Nomination operation already exists') from None

    def _contacts(self, proof, actor):
        if (type(proof) is not VerifiedContacts or proof.account_id != actor.account_id
                or proof.scope != self.scope or proof.email_verified is not True or proof.phone_verified is not True
                or type(proof.expires_at) is not int or proof.expires_at <= int(time.time())
                or proof.expires_at > actor.expires_at):
            raise MediaError(403, 'Fresh trusted same-account verified contacts required')
        self._tag(proof.email_tag); self._tag(proof.phone_tag)
