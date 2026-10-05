"""Finite app-owned current catalog reads, never installed selection or authority.

The protected native settings owner must supply the exact HTTPS origin; limits come from the existing lease bound.
No runtime setting, provider, listener or catalog wire dispatch is selected here.
"""
import time
from psycopg import sql
from psycopg.rows import dict_row
from .media import MediaError
from .nomination_contract import FrozenModel,Scope,MAX_LEASE,valid_identity_id
from .nomination_transport import BoundedReceiptReader


class CatalogSettings(FrozenModel):
    scope: Scope
    app_origin: str


class NominationCatalogCallbacks:
    def __init__(self,*,owner,settings_provider=None,require_selection=None,receipt_connect=None):
        self.owner=owner;self.settings_provider=settings_provider;self.require_selection=require_selection
        self.reader=BoundedReceiptReader(owner=owner,connect=receipt_connect) if receipt_connect else None

    def _settings(self):
        if self.settings_provider is None or self.require_selection is None or self.reader is None:
            raise MediaError(503,'Protected catalog owner unavailable')
        try:
            settings=self.settings_provider()
            if type(settings) is not CatalogSettings:raise ValueError()
            CatalogSettings.model_validate(settings.model_dump(mode='python'))
            if settings.scope!=self.owner.scope():raise ValueError()
            # Shared origin validation is authoritative; no hostname inference.
            from node_agent_package_helper.nomination_contract import NominationOwnerState as State
            State.exact_https_origin(settings.app_origin)
            return settings
        except Exception:raise MediaError(503,'Protected catalog settings unavailable') from None

    def read_scope(self,app_id):
        from node_agent_package_helper.nomination_contract import Scope as CommonScope
        if app_id!='shadify_api':raise MediaError(403,'Catalog scope denied')
        return CommonScope.model_validate(self._settings().scope.model_dump(mode='json'))

    def _guard(self,selection,settings):
        from node_agent_package_helper.nomination_reader import InstalledSelection
        from node_agent_package_helper.nomination_contract import Scope as CommonScope
        if (type(selection) is not InstalledSelection or selection.scope!=CommonScope.model_validate(
                settings.scope.model_dump(mode='json'))):raise MediaError(403,'Catalog scope denied')
        if self.require_selection(selection) is not None:raise MediaError(403,'Catalog selection denied')
        self.owner.require_current(settings.scope)
        if self._settings()!=settings:raise MediaError(409,'Catalog state changed')

    def _snapshot(self,receipt_connect):
        with receipt_connect() as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            with connection.cursor(row_factory=dict_row) as c:
                schema=c.execute('SELECT pg_catalog.current_schema() AS schema').fetchone()['schema']
                if type(schema) is not str:raise MediaError(503,'Catalog state unavailable')
                tables=[sql.Identifier(schema,name) for name in ('shadify_app_admin','shadify_users',
                    'shadify_nomination_control','shadify_admin_nomination_v1')]
                rows=c.execute(sql.SQL('SELECT a.revision AS admin_revision,a.primary_user_id,'
                    'u.auth_account_id,u.first_admitted_at,u.admission_source,'
                    'slot.nomination_id,n.id,n.scope,n.revision,n.state,n.expires_at,'
                    'n.expected_admin_revision,n.expected_primary_account_id '
                    'FROM {} a LEFT JOIN {} u ON u.id=a.primary_user_id '
                    'CROSS JOIN {} slot LEFT JOIN {} n ON n.id=slot.nomination_id '
                    'WHERE a.singleton AND slot.singleton').format(*tables)).fetchall()
        return rows

    def _read(self,selection,observed_at,*,receipt_connect):
        from node_agent_package_helper.nomination_contract import NominationOwnerState
        from node_agent_package_helper.nomination_result_transport import normalize_catalog_state
        settings=self._settings();self._guard(selection,settings)
        rows=self._snapshot(receipt_connect)
        if len(rows)!=1:raise MediaError(503,'Catalog state unavailable')
        row=rows[0];primary=row['auth_account_id']
        if row['primary_user_id'] is not None and (not valid_identity_id(primary)
                or row['first_admitted_at'] is None or row['admission_source']!='canonical_shell'):
            raise MediaError(503,'Catalog state unavailable')
        nomination=None;deadline=observed_at+30;now=int(time.time())
        if row['nomination_id'] is not None:
            if row['id'] is None or row['scope']!=settings.scope.model_dump(mode='json'):
                raise MediaError(503,'Catalog state unavailable')
            if row['state']=='pending':
                timestamp=row['expires_at'].timestamp()
                expiry=int(timestamp)
                if expiry!=timestamp:raise MediaError(503,'Catalog state unavailable')
                nomination=dict(id=str(row['id']),revision=row['revision'],status='pending',expires_at=expiry)
                actions=['revoke','replace']
                if expiry>now:
                    deadline=min(deadline,expiry)
                    if row['expected_admin_revision']==row['admin_revision'] and row['expected_primary_account_id']==primary:
                        actions.insert(0,'renew')
            elif row['state'] in ('claimed','revoked'):
                actions=['initialize' if primary is None else 'recover']
            else:
                # Shared DTO cannot represent stored blocked/expired/replaced state.
                # Deny rather than hide an outstanding nomination or mislabel it.
                raise MediaError(503,'Catalog state unavailable')
        else:actions=['initialize' if primary is None else 'recover']
        state=NominationOwnerState(schema_version=1,scope=selection.scope,app_origin=settings.app_origin,
            observed_at=observed_at,expires_at=deadline,admin_revision=row['admin_revision'],
            primary_account_id=primary,validity_limits=dict(minimumMinutes=1,
                maximumMinutes=MAX_LEASE//60),actions=actions,nomination=nomination)
        self._guard(selection,settings)
        # Preserve the finite conservative stability check across the blocking
        # provider guard. Drift between these coherent observations is unavailable.
        # This is snapshot comparison, not an atomic commitment: writers may change
        # state during cleanup or after the final SELECT. No reread loop or renewed
        # observation deadline turns catalog metadata into mutation authority.
        if self._snapshot(receipt_connect)!=rows:
            raise MediaError(409,'Catalog state changed')
        return normalize_catalog_state(state,selection.scope,now=time.time)

    async def read_owner_state(self,selection):
        """Bounded coherent observation; state is not locked until response."""
        if self.reader is None:raise MediaError(503,'Protected catalog owner unavailable')
        from functools import partial
        try:
            return await self.reader._run(partial(self._read,selection,int(time.time())))
        except MediaError:raise
        except Exception:raise MediaError(503,'Catalog state unavailable') from None

    async def require_owner_current(self,selection,expected):
        """One fresh owner read must match; None means matched at that read.

        Original expiry survives cleanup/awaits. This never authorizes mutation
        or promises unchanged state after the read or until a response is sent.
        """
        from node_agent_package_helper.nomination_result_transport import normalize_catalog_state,same_catalog_state
        from node_agent_package_helper.nomination_contract import NominationOwnerState
        try:
            if type(expected) is not NominationOwnerState:raise ValueError()
            normalize_catalog_state(expected,selection.scope,now=time.time)
            current=await self.read_owner_state(selection)
            normalize_catalog_state(expected,selection.scope,now=time.time)
            if not same_catalog_state(expected,current):raise ValueError()
        except Exception:raise MediaError(409,'Catalog state changed or unavailable') from None
        return None
