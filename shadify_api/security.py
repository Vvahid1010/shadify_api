"""Shadify-owned endpoint catalog; transport/keys/replay stay canonical."""
HEADERS = ["accept", "content-type", "cache-control", "x-auth-assertion",
           "x-authenticated-account-id", "x-authenticated-session-id",
           "x-authenticated-auth-level", "x-authenticated-domain-app", "x-authenticated-proxy-app",
           "x-request-id", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"]


def receiver_catalog():
    routes = [("me.read", "GET", "/api/me"),
              ("tracks.read", "GET", "/api/artists/{artist_id}/tracks"),
              ("tracks.create", "POST", "/api/artists/{artist_id}/tracks"),
              ("media.upload.create", "POST", "/api/media/uploads"),
              ("media.upload.complete", "POST", "/api/media/uploads/{asset_id}/complete"),
              ("media.read", "GET", "/api/media/{asset_id}")]
    return [dict(endpoint_id="shadify_api." + name, receiver_app="shadify_api",
                 permitted_peer_apps=["authentication"], method=method, path=path,
                 headers=list(HEADERS), off_supported=True, proxy_headers=True)
            for name,method,path in routes]


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
        if type(document) is not dict or document.get("profile") != "local_unix_v1":
            raise ValueError("Shadify local Unix receiver required")
        self.binding, self.bounds, self.active = binding, bounds, True
        self.channel = install_configured_receiver(binding, document, policy,
                              clock_bounds=bounds.clock_ready, storage_bounds=bounds.storage_ready)
        from node_agent_local_shell_transport.local_uds import LocalUnixBinding
        if type(self.channel) is not LocalUnixBinding:
            raise ValueError("Shadify local Unix adapter unavailable")

    def current(self):
        # Immutable process-owned selection, compared to the actual native
        # context. No request-supplied selector or fabricated digest is accepted.
        try:
            native = self.binding.acquire()
            return (self.active and native.status()[:2] == ("shadify_api", self.channel.recipient_app_instance_id)
                    and native.revisions() == [(self.channel.connection_id, self.channel.policy_revision, self.channel.policy_digest)])
        except Exception:
            return False

    def http_protocol(self, *args, **kwargs):
        from node_agent_local_shell_transport.local_uds import ingress_protocol
        return ingress_protocol(self.channel, current=self.current)(*args, **kwargs)

    def readiness(self):
        return {"managed_ingress": "installed" if self.current() else "selection_unavailable",
                "clock_bounds": "ready" if self.bounds.clock_ready() else "unavailable",
                "replay_storage_bounds": "ready" if self.bounds.storage_ready() else "unavailable"}

    def close(self):
        self.active = False
        self.bounds.close()
