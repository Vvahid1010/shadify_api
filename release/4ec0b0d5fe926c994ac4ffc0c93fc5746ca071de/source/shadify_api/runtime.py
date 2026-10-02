"""Explicit native composition. No migrations, listeners, cloud probes or secret discovery."""
import os
import asyncio
import hashlib
import secrets
import time
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, SecretStr

from .auth import require_account
from .config import load_protected_json, load_database_config, load_storage_config
from .domain import DomainService
from .media import MediaService
from .repository import PostgresMediaRepository
from .security import create_binding, ManagedReceiver
from .storage import R2Storage


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    app_id: Literal["shadify_api"] = "shadify_api"
    database_file: Literal["database.credentials.json"] = "database.credentials.json"
    storage_file: Literal["storage.credentials.json"] = "storage.credentials.json"
    app_security: dict
    app_security_binding: dict
    app_security_transport: dict | None = None


def replay_address(directory):
    """Canonical ON replay on the assigned existing Redis allocation; no queue."""
    value = load_protected_json(directory / "replay.credentials.json")
    if set(value) != {"url"} or type(value["url"]) is not str:
        raise ValueError("Replay configuration invalid")
    from urllib.parse import urlsplit
    url = urlsplit(value["url"])
    if (url.scheme not in {"redis", "rediss"} or not url.hostname or not url.password
            or (url.scheme == "redis" and url.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Replay allocation requires protected credentials and remote TLS")
    return SecretStr(value["url"])


def replay_factory(directory, address=None):
    address = address or replay_address(directory)
    def factory(instance, clock_bounds, storage_bounds):
        from redis.asyncio import Redis
        from app_security_shell.replay import RedisReplayStore
        client = Redis.from_url(address.get_secret_value(), socket_connect_timeout=2,
                                socket_timeout=3, retry_on_timeout=False)
        store = RedisReplayStore(client, "app-security:replay:" + instance,
                                 clock_bounds, storage_bounds=storage_bounds)
        return store, client.aclose
    return factory


def create_runtime_app(directory: Path, *, binding=None):
    directory = Path(directory)
    receiver = None
    try:
        config = RuntimeConfig.model_validate(load_protected_json(directory / "shadify_api.config.json"))
        profile = load_protected_json(directory / "app_profile.json")
        if profile.get("schema_version") != 1 or profile.get("app") != "shadify_api":
            raise ValueError("Shadify app profile required")
        repository = None
        if (directory / config.database_file).exists():
            database = load_database_config(directory / config.database_file)
            repository = PostgresMediaRepository(database.connect)
        if binding is None:
            address = replay_address(directory) if (directory / "replay.credentials.json").exists() else None
            factory = replay_factory(directory, address) if address is not None else None
            projection = {"app_security": config.app_security, "app_security_binding": config.app_security_binding}
            binding = create_binding(projection, directory, factory)
            if config.app_security_transport is not None:
                if address is None:
                    raise ValueError("Protected replay allocation required for managed receiver")
                from .replay_bounds import LocalReplayBounds
                bounds = LocalReplayBounds(address.get_secret_value())
                try:
                    receiver = ManagedReceiver(binding, config.app_security_transport, config.app_security, bounds)
                except Exception:
                    bounds.close()
                    raise
    except Exception:
        raise RuntimeError("Shadify runtime configuration invalid; values redacted") from None
    # Storage can remain absent during Auth/profile/database preparation. Never read
    # secret values at import time and never inherit an ambient AWS credential chain.
    service = None
    if (directory / config.storage_file).exists():
        try:
            storage = load_storage_config(directory / config.storage_file)
            if repository is not None:
                service = MediaService(repository, R2Storage(storage), storage.max_upload_bytes, storage.upload_ttl_seconds)
        except Exception:
            if receiver is not None:
                receiver.close()
            raise RuntimeError("Shadify storage configuration invalid; values redacted") from None

    async def readiness():
        checks = {"authentication": binding.status().get("state"),
                  "database": "unavailable" if repository else "missing",
                  "storage": "configured_unverified" if service else "missing"}
        checks["replay_admission"] = "unavailable"
        if checks["authentication"] == "Active" and receiver is not None and receiver.current():
            try:
                # A disposable probe uses the canonical reservation/recovery gate;
                # no business dispatch, release, bespoke replay logic or background job.
                now = int(time.time())
                await binding.reserve(hashlib.sha256(secrets.token_bytes(32)).hexdigest(), now + 1, now)
                checks["replay_admission"] = "ready"
            except Exception:
                checks["replay_admission"] = "recovering_or_unavailable"
        checks.update(receiver.readiness() if receiver is not None else {"managed_ingress": "missing",
                      "clock_bounds": "unavailable", "replay_storage_bounds": "unavailable"})
        if repository is not None:
            try:
                await asyncio.to_thread(repository.ready)
                checks["database"] = "schema_ready"
            except Exception:
                pass
        ready = (checks["authentication"] == "Active" and checks["database"] == "schema_ready"
                 and service is not None and checks["managed_ingress"] == "installed"
                 and checks["clock_bounds"] == "ready" and checks["replay_storage_bounds"] == "ready"
                 and checks["replay_admission"] == "ready")
        # Metadata can be served independently while overall upload readiness is
        # pending. Configured storage remains explicitly unverified cloud access.
        return ready, checks

    from .main import create_app
    return create_app(service, require_account, domain=DomainService(repository) if repository else None,
                      repository=repository, binding=binding, readiness=readiness, receiver=receiver)


def app_factory():
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise RuntimeError("CREDENTIALS_DIRECTORY required")
    return create_runtime_app(Path(directory))


def _entrypoint():
    # Standard native ASGI target. Without a selected projected directory the
    # process can expose liveness, but has no trusted identity or business wiring.
    if os.environ.get("CREDENTIALS_DIRECTORY"):
        return app_factory()
    from .main import create_app
    return create_app()


app = _entrypoint()


def http_protocol(*args, **kwargs):
    """Node Agent's fixed Uvicorn --http shadify_api.runtime:http_protocol target."""
    receiver = getattr(app.state, "managed_receiver", None)
    if receiver is None:
        raise RuntimeError("Shadify local receiver configuration required")
    return receiver.http_protocol(*args, **kwargs)
