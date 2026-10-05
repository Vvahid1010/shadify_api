"""Optional protected Node composition. No default endpoint, listener or selection."""
import asyncio
from contextlib import contextmanager
from functools import partial
from threading import Event, Lock

from .nomination_contract import Witness
from .media import MediaError


class _ReceiptJob:
    def __init__(self, connect):
        self.connect = connect
        self.stopped = Event()
        self.lock = Lock()
        self.connection = None

    @contextmanager
    def opened(self):
        # The trusted owner connector has connect_timeout=1. A cancellation
        # during connect cannot reach libpq yet, so fence its bounded completion.
        connection = self.connect()
        try:
            with self.lock:
                self.connection = connection
            if self.stopped.is_set():
                raise RuntimeError('Receipt cancelled before query')
            with connection:
                connection.execute('SET LOCAL statement_timeout=1000')
                connection.execute('SET LOCAL lock_timeout=500')
                yield connection
        finally:
            with self.lock:
                self.connection = None
            connection.close()

    def cancel(self):
        self.stopped.set()
        with self.lock:
            connection = self.connection
        if connection is not None:
            try:
                connection.cancel_safe(timeout=1)
            except Exception:
                # Even a failed/racing cancel is bounded by SQL timeout. The
                # caller still joins the worker, including rollback and close.
                pass


async def _finish(task):
    """Fence cleanup even if the surrounding transport cancels repeatedly."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        try:
            task.result()
        except Exception:
            pass


class BoundedReceiptReader:
    def __init__(self, *, owner, connect):
        self.owner, self.connect = owner, connect

    async def __call__(self, scope, operation_id):
        job = _ReceiptJob(self.connect)
        # Executor futures are not separate cancellable asyncio Tasks during
        # graceful loop shutdown. Shield and join the actual worker completion.
        loop = asyncio.get_running_loop()
        worker = loop.run_in_executor(None, partial(
            self.owner.read_committed_operation, scope, operation_id,
            receipt_connect=job.opened))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel = loop.run_in_executor(None, job.cancel)
            await _finish(cancel)
            await _finish(worker)
            raise


class LazyNominationProvider:
    """Native receiver is current after startup, not during app construction."""
    def __init__(self, factory):
        self.factory, self.lock, self.provider = factory, Lock(), None

    def get(self):
        with self.lock:
            if self.provider is None:
                self.provider = self.factory()
            return self.provider

    def require_current(self, scope, witness=None):
        return self.get().require_current(scope, witness)

    def read_verified_witness(self, scope, operation_id):
        return self.get().read_verified_witness(scope, operation_id)


class ProtectedNominationComposition:
    """Server-owned descriptor/peer/installed callbacks; never request fields.

    The Node installer supplies an authenticated reader descriptor connector and
    endpoint provenance guard: fixed path/non-app-writable parents, socket
    owner/mode/inode stability and trusted systemd unit/launcher/module/release.
    SO_PEERCRED of an inherited listener is not child-reader PID identity.
    read_installed is its ProtectedInstalledSelectionReader. No root-UID shortcut or path guess.
    Native receiver/instance scope remains the API runtime's own live guard.
    """
    def __init__(self, *, connect_descriptor, require_reader_peer,
                 read_installed, management, require_control):
        self.connect_descriptor = connect_descriptor
        self.require_reader_peer = require_reader_peer
        self.read_installed = read_installed
        self.management = management
        self.require_control = require_control
        self.adapter = None
        self._initialize = None
        self._endpoint_guard = None

    @classmethod
    def from_endpoint(cls, *, endpoint_guard, read_installed, management, require_control):
        """Consume Node's exact approved guard; never pin first observations.

        Node ownership supplies the protected expected EndpointSnapshot and
        bounded actual loaded-unit/sealed-release observer. This factory cannot
        construct those from app settings, request JSON or a responding peer.
        """
        from node_agent_package_helper.nomination_endpoint import ProtectedReaderEndpointGuard
        from node_agent_package_helper.nomination_reader import Denied
        if type(endpoint_guard) is not ProtectedReaderEndpointGuard:
            raise Denied()
        composition = cls(connect_descriptor=endpoint_guard.connect,
            require_reader_peer=endpoint_guard.require_peer, read_installed=read_installed,
            management=management, require_control=require_control)
        composition._endpoint_guard = endpoint_guard
        return composition

    @classmethod
    def from_installed_endpoint(cls, *, read_installed, management, require_control):
        """Use Node's real protected record/loaded-unit/seal composition.

        Explicit installer construction only. No observer, snapshot, group,
        socket path or permissive fallback is accepted from app/wire settings.
        Absent/unreadable installation/access fails through Node's exact factory.
        """
        from node_agent_package_helper.nomination_endpoint_installation import compose_endpoint_guard
        return cls.from_endpoint(endpoint_guard=compose_endpoint_guard(),
            read_installed=read_installed, management=management, require_control=require_control)

    def prepare(self, owner, scope_provider, receipt_connect, *, native_identity_provider):
        if self._initialize is not None or self.adapter is not None:
            raise MediaError(503, 'Protected composition already selected')
        lazy = LazyNominationProvider(lambda: self.bind(owner, scope_provider, receipt_connect,
            native_identity_provider=native_identity_provider))
        self._initialize = lazy.get
        return lazy

    def bind(self, owner, scope_provider, receipt_connect, *, native_identity_provider):
        # Optional Node dependency is not imported or activated by normal boot.
        from node_agent_package_helper.nomination_contract import Scope
        from node_agent_package_helper.nomination_reader import InstalledSelection, Denied
        from node_agent_package_helper.nomination_provider import NominationProviderClient
        from node_agent_package_helper.nomination_app_adapter import AppProviderAdapter
        owner_scope = scope_provider()
        common = Scope.model_validate(owner_scope.model_dump(mode='json'))
        selection = self.read_installed(common)
        if type(selection) is not InstalledSelection or selection.scope != common:
            raise Denied()

        def selected(scope):
            expected_identity = (selection.node_id, common.app_id, common.app_instance_id)
            if (scope != common or scope_provider() != owner_scope
                    or native_identity_provider() != expected_identity
                    or self.read_installed(common) != selection
                    or native_identity_provider() != expected_identity
                    or scope_provider() != owner_scope):
                raise Denied()

        def selected_owner(scope):
            if scope != owner_scope:
                raise Denied()
            selected(common)

        def map_witness(proof):
            body = proof.model_dump(mode='json')
            body.pop('issued_at')  # Node checks/retains the full original proof.
            return Witness.model_validate(body)

        selected(common)
        if self._endpoint_guard is not None:
            from node_agent_package_helper.nomination_endpoint import client_for_endpoint
            client = client_for_endpoint(scope=common, guard=self._endpoint_guard,
                require_selection=selected)
        else:
            # Retain explicit trusted callback construction for direct source
            # fixtures and prior owner integration; never infer activation peers.
            client = NominationProviderClient(scope=common, connect=self.connect_descriptor,
                require_peer=self.require_reader_peer, require_selection=selected)
        receipt = BoundedReceiptReader(owner=owner, connect=receipt_connect)
        adapter = AppProviderAdapter(client=client, owner_scope=owner_scope,
            common_scope=common, map_witness=map_witness,
            operation_id=lambda proof: proof.operation_id,
            require_owner_selection=selected_owner, read_owner_result=receipt)
        self.selection, self._selected, self.owner_scope = selection, selected, owner_scope
        self.adapter = adapter
        return adapter

    async def serve_receipt(self, connection):
        """One connected, protected app-owned descriptor; always close it."""
        try:
            if self.adapter is None and self._initialize is not None:
                await asyncio.to_thread(self._initialize)
            if self.adapter is None:
                raise MediaError(503, 'Protected nomination composition unavailable')
            from node_agent_package_helper.nomination_result_transport import serve_result_async

            def selected(selection):
                if selection != self.selection:
                    raise MediaError(409, 'Installed nomination selection changed')
                self._selected(selection.scope)

            async def receipt(scope, operation_id):
                if scope != self.selection.scope:
                    raise MediaError(403, 'Nomination scope mismatch')
                return await self.adapter.read_operation_result(self.owner_scope, operation_id)

            return await serve_result_async(connection, selection=self.selection,
                management=self.management, read_receipt=receipt,
                require_selection=selected, require_control=self.require_control)
        finally:
            connection.close()
