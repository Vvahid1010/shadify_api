# Shadify API

Source-only FastAPI/PostgreSQL metadata and private R2 upload foundation.
Existing Authentication owns identity; no local login or live cloud runtime.

- [Implementation/configuration and pending proof](docs/API_FOUNDATION.md)
- [Native WSL contract](local_bootstrap.md)
- [Storage architecture](docs/MEDIA_STORAGE_ARCHITECTURE.md)
- [Delivery/cache/security](docs/MEDIA_DELIVERY_CACHE_SECURITY.md)

Tests: native WSL `.venv/bin/python -m unittest discover -s tests -v`;
exact validated dependency pins are in `requirements.lock`. No cloud credentials or network are required.
