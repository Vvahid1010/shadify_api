"""Finite editorial endpoint addition; does not replace existing receiver entries."""
def editorial_receiver_catalog(headers):
    routes = (
        ('discover.editorial.public', 'GET', '/api/discover/editorial'),
        ('discover.editorial.read', 'GET', '/api/admin/discover'),
        ('discover.editorial.draft', 'PATCH', '/api/admin/discover/draft'),
        ('discover.editorial.submit', 'POST', '/api/admin/discover/submit'),
        ('discover.editorial.publish', 'POST', '/api/admin/discover/publish'),
        ('discover.editorial.candidates', 'GET', '/api/admin/discover/candidates'),
        ('discover.editorial.editor.grant', 'PUT', '/api/admin/discover/editors/{account_id}'),
        ('discover.editorial.editor.revoke', 'DELETE', '/api/admin/discover/editors/{account_id}'),
    )
    return [dict(endpoint_id='shadify_api.'+name,receiver_app='shadify_api',
        permitted_peer_apps=['authentication'],method=method,path=path,
        headers=list(headers),off_supported=True,proxy_headers=True,
        **({'max_response_bytes':8388608} if method=='GET' else {}))
        for name,method,path in routes]
