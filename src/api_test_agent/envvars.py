"""Read a secret from the environment without ever printing or storing it.

The process environment wins. On Windows a variable that was set for the user after this process
started (the usual case: ``setx`` while the shell was already open) is read from the user's
environment in the registry. Only the one variable the configuration names is looked up.
"""

from __future__ import annotations

import os


def read_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            data, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(data) or None
