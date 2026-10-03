"""Synthetic admitted context for isolated tests; never operational authority."""
import time
from shadify_api.auth import Identity


def admitted_actor(account, *, admin=None, issued=None, expires=None):
    now = int(time.time())
    return Identity(account, "test-session", "mfa", now + 300 if expires is None else expires,
                    now if issued is None else issued, "shadify", "shadify_api",
                    account == "admin" if admin is None else admin)
