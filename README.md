# Shadify API

Private authenticated user/owned-track metadata and R2 original upload API.
Existing Authentication and the canonical security shell own trusted identity;
PostgreSQL owns domain metadata, R2 owns media bytes.

- [Current endpoints, protected config and remaining live prerequisites](docs/API_FOUNDATION.md)
- [Native development/package contract](local_bootstrap.md)
- [Storage architecture](docs/MEDIA_STORAGE_ARCHITECTURE.md)
- [Media delivery/cache/security](docs/MEDIA_DELIVERY_CACHE_SECURITY.md)

Native WSL checks: `env -u CREDENTIALS_DIRECTORY .venv/bin/python -m unittest discover -s tests -v`.
Pinned registry packages are in requirements.lock; canonical private shell
provenance is in app_security_dependency.json. No cloud credentials are needed
for offline tests. No DB migration/service/cloud activation is implied by source
or build publication.
