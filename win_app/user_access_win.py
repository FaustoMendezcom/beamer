"""What the signed-in user could do without elevation. An elevated Beamer acts on paths that came
from elsewhere (the clipboard, Send to, a peer's drop), and must not read or write anything there
that the user's own, unelevated account could not: else any program of the user could have it
read protected files out to a peer, or a peer could plant files in System32.

Checked with AccessCheck against the token Windows links to the elevated one (the filtered,
standard-rights token of the same sign-in). Not elevated, Beamer already has just those rights,
and every check passes."""

import ctypes
import logging
import sys
from typing import Optional

LOGGER = logging.getLogger(__name__)
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_EXECUTE = 0x001200A0
FILE_ALL_ACCESS = 0x001F01FF
FILE_ADD_FILE = 0x0002
FILE_ADD_SUBDIRECTORY = 0x0004
MAPPING = (FILE_GENERIC_READ, FILE_GENERIC_WRITE, FILE_GENERIC_EXECUTE, FILE_ALL_ACCESS)
TOKEN_LINKED_TOKEN = 19
TOKEN_ELEVATION = 20
TOKEN_QUERY = 0x0008

_token = None
_checked = False


def _user_token():
    """The unelevated token linked to this elevated one, at identification level; None when this
    process is not elevated (its own rights are the user's)."""
    global _token, _checked
    if _checked:
        return _token
    _checked = True
    if sys.platform != "win32":
        return None
    import win32api
    import win32security

    own = win32security.OpenProcessToken(win32api.GetCurrentProcess(), TOKEN_QUERY)
    if not win32security.GetTokenInformation(own, TOKEN_ELEVATION):
        return None
    linked = win32security.GetTokenInformation(own, TOKEN_LINKED_TOKEN)
    _token = win32security.DuplicateToken(linked, win32security.SecurityIdentification)
    return _token


def _other_account_profile() -> Optional[str]:
    """The signed-in user's profile when Beamer runs as another account (elevated with an
    administrator's credentials): then this process's linked token is the administrator's, which
    says nothing of the signed-in user's rights."""
    try:
        import shell_folder_win

        user = shell_folder_win.session_user()
    except Exception:
        return None
    return user[1] if user else None


def _inside(path: str, folder: str) -> bool:
    import os

    try:
        return os.path.commonpath([os.path.realpath(path).lower(), os.path.realpath(folder).lower()]) \
            == os.path.realpath(folder).lower()
    except ValueError:
        return False


def allowed(path: str, access: int) -> bool:
    """Whether the signed-in user's unelevated account has `access` to `path`. Beamer running as
    another account cannot ask Windows for that user's rights, so it holds to that user's own
    profile folder instead. False when it cannot be told."""
    profile = _other_account_profile()
    if profile is not None:
        return _inside(path, profile)
    try:
        token = _user_token()
    except Exception:
        LOGGER.exception("Could not read the unelevated token")
        return False
    if token is None:
        return True
    try:
        return _access_check(path, int(token), access)
    except Exception:
        LOGGER.exception("Could not check access to %s", path)
        return False


class _GenericMapping(ctypes.Structure):
    _fields_ = [("read", ctypes.c_ulong), ("write", ctypes.c_ulong), ("execute", ctypes.c_ulong),
                ("all", ctypes.c_ulong)]


def _access_check(path: str, token: int, access: int) -> bool:
    """advapi32 AccessCheck (pywin32 has none) on `path`'s owner, group and DACL for `token`."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    information = 0x1 | 0x2 | 0x4  # OWNER, GROUP, DACL
    needed = ctypes.c_ulong(0)
    advapi32.GetFileSecurityW(ctypes.c_wchar_p(path), information, None, 0, ctypes.byref(needed))
    if not needed.value:
        return False
    descriptor = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetFileSecurityW(ctypes.c_wchar_p(path), information, descriptor, needed, ctypes.byref(needed)):
        return False
    mapping = _GenericMapping(*MAPPING)
    privileges = ctypes.create_string_buffer(256)
    privileges_size = ctypes.c_ulong(256)
    granted, status = ctypes.c_ulong(0), ctypes.c_int(0)
    if not advapi32.AccessCheck(descriptor, ctypes.c_void_p(token), ctypes.c_ulong(access), ctypes.byref(mapping),
                                privileges, ctypes.byref(privileges_size), ctypes.byref(granted), ctypes.byref(status)):
        return False
    return bool(status.value) and (granted.value & access) == access


def can_read(path: str) -> bool:
    return allowed(path, FILE_GENERIC_READ)


def can_add_to(folder: Optional[str]) -> bool:
    return bool(folder) and allowed(folder, FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY)
