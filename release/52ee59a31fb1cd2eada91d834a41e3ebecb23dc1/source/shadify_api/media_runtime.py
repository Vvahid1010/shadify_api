"""Dedicated byte-listener factory; no canonical receiver created in this process."""
import os
from pathlib import Path
from .config import load_database_config, compose_storage
from .repository import PostgresMediaRepository

def app_factory():
    """Separate Node-owned capability-only listener, never a guarded API bypass."""
    from .media_routes import create_media_app
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory: raise RuntimeError('CREDENTIALS_DIRECTORY required')
    directory = Path(directory)
    try:
        database = load_database_config(directory / 'database.credentials.json')
        repository = PostgresMediaRepository(database.connect)
        storage, _ = compose_storage(directory, repository)
        return create_media_app(storage)
    except Exception:
        raise RuntimeError('Media listener configuration invalid; values redacted') from None
