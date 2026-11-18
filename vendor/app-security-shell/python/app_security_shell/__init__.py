"""Common application security; importing its guard never requires native code."""
def protocol_version():
    from ._native import protocol_version as native_version
    return native_version()

__all__ = ['protocol_version']
