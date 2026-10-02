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
