"""Shadify-owned endpoint catalog; transport/keys/replay stay canonical."""
HEADERS = ["accept", "content-type", "cache-control", "x-auth-assertion",
           "x-authenticated-account-id", "x-authenticated-session-id",
           "x-authenticated-auth-level", "x-authenticated-domain-app", "x-authenticated-proxy-app",
           "x-proxy-approval", "x-request-id", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"]


def receiver_catalog():
    routes = [("me.read", "GET", "/api/me"),
              ("tracks.read", "GET", "/api/artists/{artist_id}/tracks"),
              ("tracks.create", "POST", "/api/artists/{artist_id}/tracks"),
              ("media.upload.create", "POST", "/api/media/uploads"),
              ("media.upload.complete", "POST", "/api/media/uploads/{asset_id}/complete"),
              ("artists.profile_media.upload", "POST", "/api/artists/{artist_id}/profile-media/uploads"),
              ("artists.profile_media.access", "GET", "/api/artists/{artist_id}/profile-media/{purpose}/access"),
              ("media.read", "GET", "/api/media/{asset_id}"),
              ("media.access", "POST", "/api/media/{asset_id}/access"),
              ("media.playback", "POST", "/api/tracks/{track_id}/playback")]
    routes += [("releases.cover.access", "GET", "/api/releases/{release_id}/cover/access"),
               ("tracks.cover.access", "GET", "/api/tracks/{track_id}/cover/access")]
    routes += [("artists.catalog", "GET", "/api/artist-catalog")]
    routes += [
        ("admin.nomination", "POST", "/api/admin/nominations"),
        ("admin.nomination.context", "GET", "/api/admin/nominations/context"),
        ("admin.authority", "GET", "/api/admin/authority"),
        ("admin.audit", "GET", "/api/admin/authority/audit"),
        ("admin.delegate", "PUT", "/api/admin/delegates/{account_id}"),
        ("admin.delegate.revoke", "DELETE", "/api/admin/delegates/{account_id}"),
        ("admin.primary", "PUT", "/api/admin/primary"),
        ("artists.mine", "GET", "/api/me/artists"),
        ("artists.create", "POST", "/api/admin/artists"),
        ("artists.public.slug", "GET", "/api/artist-slugs/{slug}/public"),
        ("artists.public.id", "GET", "/api/artists/{artist_id}/public"),
        ("artists.read", "GET", "/api/artists/{artist_id}"),
        ("artists.edit", "PATCH", "/api/artists/{artist_id}"),
        ("artists.layout", "PUT", "/api/artists/{artist_id}/layout"),
        ("artists.slug", "PUT", "/api/artists/{artist_id}/slug"),
        ("artists.assign", "POST", "/api/admin/artists/{artist_id}/owner"),
        ("artists.transfer", "PUT", "/api/admin/artists/{artist_id}/owner"),
        ("artists.revoke", "DELETE", "/api/admin/artists/{artist_id}/owner"),
        ("artists.publication", "PUT", "/api/artists/{artist_id}/publication"),
        ("artists.suspension", "PUT", "/api/admin/artists/{artist_id}/suspension"),
        ("artists.verification", "PUT", "/api/admin/artists/{artist_id}/verification"),
        ("artists.audit", "GET", "/api/admin/artists/{artist_id}/ownership-audit"),
        ("users.profile.read", "GET", "/api/admin/users/{user_id}/profile"),
        ("users.profile.edit", "PATCH", "/api/admin/users/{user_id}/profile")]
    for resource in ("tracks", "releases", "videos", "moments", "events"):
        path="/api/artists/{artist_id}/" + resource
        if resource != "tracks":
            routes += [(resource+".create", "POST", path), (resource+".list", "GET", path)]
        path += "/{content_id}"
        routes += [(resource+".item.read", "GET", path), (resource+".edit", "PATCH", path),
                   (resource+".delete", "DELETE", path)]
        if resource != "events":
            routes += [(resource+".publication", "PUT", path+"/publication")]
    routes += [
        ("artists.uploads.read", "GET", "/api/artists/{artist_id}/uploads"),
        ("media.match_review.read", "GET", "/api/media/{asset_id}/match-review"),
        ("media.match_review.edit", "PUT", "/api/media/{asset_id}/match-review")]
    catalog = [dict(endpoint_id="shadify_api." + name, receiver_app="shadify_api",
                 permitted_peer_apps=["authentication"], method=method, path=path,
                 headers=list(HEADERS), off_supported=True, proxy_headers=True)
            for name,method,path in routes]
    # Public page contains several bounded collections; private list permits up
    # to 100 bounded records. Declare the existing shell's supported 8 MiB bound.
    for endpoint in catalog:
        if endpoint['method']=='GET' and (endpoint['path']=='/api/artist-catalog' or endpoint['path'].endswith('/public')
                or endpoint['path'].rsplit('/',1)[-1] in {'tracks','releases','videos','moments','events'}):
            endpoint['max_response_bytes']=8388608
    from pathlib import Path
    import json
    catalog.extend(json.loads(Path(__file__).with_name('manual_search_receiver_catalog.json').read_text()))
    from .editorial_catalog import editorial_receiver_catalog
    catalog.extend(editorial_receiver_catalog(HEADERS))
    from .public import public_receiver_catalog
    catalog.extend(public_receiver_catalog())
    return catalog



def create_binding(projection, directory, replay_factory=None):
    from app_security_shell.binding import AppSecurityBinding
    if projection["app_security"]["local"]["app_id"] != "shadify_api":
        raise ValueError("Shadify security identity required")
    return AppSecurityBinding(snapshot_provider=lambda: projection,
                              directory_provider=lambda: str(directory),
                              receiver_catalog=receiver_catalog(), replay_factory=replay_factory)


class ManagedReceiver:
    """Thin wiring to the owner-supplied canonical local Unix adapter."""
    def __init__(self, binding, document, policy, bounds):
        from node_agent_local_shell_transport.managed_uds import install_configured_receiver
        if type(document) is not dict or document.get("profile") not in {"local_unix_v1", "local_unix_wsl_v1"}:
            raise ValueError("Shadify local Unix receiver required")
        self.boundary = None
        self._uid = None
        if document["profile"] == "local_unix_wsl_v1":
            import os
            from node_agent_local_shell_transport.wsl_uds import configured_boundary
            from node_agent_local_shell_transport.wsl_uds import install_configured_receiver
            self.boundary, self._uid = configured_boundary(document), os.geteuid
        self.binding, self.bounds, self.active = binding, bounds, True
        self._clock_healthy = self._storage_healthy = False
        options = {"boundary": self.boundary} if self.boundary is not None else {}
        self.channel = install_configured_receiver(binding, document, policy,
                              clock_bounds=self.clock_ready, storage_bounds=self.storage_ready, **options)
        from node_agent_local_shell_transport.local_uds import LocalUnixBinding
        if type(self.channel) is not LocalUnixBinding:
            raise ValueError("Shadify local Unix adapter unavailable")

    def clock_ready(self):
        try:
            self._clock_healthy = self.bounds.clock_ready() is True
        except Exception:
            self._clock_healthy = False
        return self._clock_healthy

    def storage_ready(self):
        try:
            self._storage_healthy = self.bounds.storage_ready() is True
        except Exception:
            self._storage_healthy = False
        return self._storage_healthy

    def current(self):
        # Immutable process-owned selection, compared to the actual native
        # context. No request-supplied selector or fabricated digest is accepted.
        try:
            native = self.binding.acquire()
            return (self.active and (self.boundary is None or self._uid() == self.boundary.receiver_uid)
                    and native.status()[:2] == ("shadify_api", self.channel.recipient_app_instance_id)
                    and native.revisions() == [(self.channel.connection_id, self.channel.policy_revision, self.channel.policy_digest)])
        except Exception:
            return False

    def http_protocol(self, *args, **kwargs):
        if self.boundary is not None:
            from node_agent_local_shell_transport.wsl_uds import ingress_protocol
            return ingress_protocol(self.channel, self.boundary, current=self.current)(*args, **kwargs)
        from node_agent_local_shell_transport.local_uds import ingress_protocol
        return ingress_protocol(self.channel, current=self.current)(*args, **kwargs)

    def readiness(self):
        # Report only bounds consumed by canonical admission. In particular,
        # never consume the one-shot Redis epoch failure as a health observation.
        return {"managed_ingress": "installed" if self.current() else "selection_unavailable",
                "clock_bounds": "ready" if self._clock_healthy else "unavailable",
                "replay_storage_bounds": "ready" if self._storage_healthy else "unavailable"}

    def close(self):
        self.active = False
        self.bounds.close()


def projected_off_connection(policy):
    """Explicit WSL OFF selection; native policy validation remains authoritative."""
    if policy.get('extra_encryption_default') is not True:
        raise ValueError('Canonical encryption default required')
    connections = policy.get('connections', [])
    if len(connections) != 1:
        raise ValueError('One projected Shadify connection required')
    connection = connections[0]
    endpoints = {row['endpoint_id'] for row in receiver_catalog()}
    if (connection['recipient']['identity'] != policy['local']
            or policy['local']['app_id'] != 'shadify_api'
            or connection['sender']['identity']['app_id'] != 'authentication'
            or set(connection['endpoint_ids']) != endpoints
            or len(connection['endpoint_ids']) != len(endpoints)):
        raise ValueError('Projected Shadify identity/catalog mismatch')
    overrides = policy.get('endpoint_overrides', [])
    wanted = {(connection['connection_id'], endpoint) for endpoint in endpoints}
    seen = set()
    for row in overrides:
        if (set(row) != {'connection_id', 'endpoint_id', 'extra_encryption'}
                or row['extra_encryption'] is not False):
            raise ValueError('Projected WSL requires explicit OFF endpoints')
        key = (row['connection_id'], row['endpoint_id'])
        if key not in wanted or key in seen:
            raise ValueError('Projected OFF selection mismatch')
        seen.add(key)
    if seen != wanted:
        raise ValueError('Projected WSL requires every endpoint explicitly OFF')
    return connection


class ProjectedReceiver:
    """Reuse the established certificate-pinned mTLS receiver, WSL OFF only."""
    requires_replay = False

    def __init__(self, binding, directory, policy, binding_config):
        from node_agent_local_shell_transport.projection import ReceiverTransport, load_transport
        from app_security_shell.routing import normalized_catalog
        import copy, hashlib, json
        if binding_config != {'schema_version': 1, 'destinations': [], 'outbound_endpoints': []}:
            raise ValueError('Projected WSL receiver-only binding required')
        self.connection = copy.deepcopy(projected_off_connection(policy))
        self.local = copy.deepcopy(policy['local'])
        # Match the pinned canonical binding's public checkpoint fingerprint;
        # any policy/mode/catalog replacement makes this receiver unavailable.
        fingerprint = json.dumps([policy, binding_config, normalized_catalog(receiver_catalog())],
                                 sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        self.projection_sha256 = hashlib.sha256(fingerprint).hexdigest()
        self.transport = load_transport(directory)
        if type(self.transport) is not ReceiverTransport or len(self.transport.peers) != 1:
            raise ValueError('Projected mTLS receiver required')
        peer = self.transport.peers[0]
        sender = self.connection['sender']['identity']
        if (peer.connection_id, peer.node_id, peer.app_instance_id) != (
                self.connection['connection_id'], sender['node_id'], sender['app_instance_id']):
            raise ValueError('Projected Authentication peer mismatch')
        self.binding, self.active = binding, True
        self.transport.install(binding)

    def current(self):
        try:
            native = self.binding.acquire()
            revisions = native.revisions()
            checkpoint = self.binding.checkpoint()
            return (self.active and native.status()[:2] == ('shadify_api', self.local['app_instance_id'])
                    and checkpoint['projection_sha256'] == self.projection_sha256
                    and tuple(checkpoint['identity']) == (
                        self.local['node_id'], 'shadify_api', self.local['app_instance_id'])
                    and len(revisions) == 1 and revisions[0][:2] == (
                        self.connection['connection_id'], self.connection['policy_revision']))
        except Exception:
            return False

    def http_protocol(self, *args, **kwargs):
        return self.transport.server_options()['http'](*args, **kwargs)

    def readiness(self):
        return {'managed_ingress': 'installed' if self.current() else 'selection_unavailable',
                'clock_bounds': 'not_required_off', 'replay_storage_bounds': 'not_required_off'}

    def close(self):
        self.active = False
