"""Where dropped and sent files go on Windows: the folder of the Explorer window, or the desktop,
under a point, else Downloads\\Beamer; and the Send to entry that hands files to Beamer."""

import ctypes
import functools
import logging
import os
import sys
from typing import Optional

from core.i18n import tr

LOGGER = logging.getLogger(__name__)
GA_ROOT = 2
EXPLORER_CLASSES = {"CabinetWClass", "ExploreWClass"}
DESKTOP_CLASSES = {"Progman", "WorkerW"}
SEND_TO_NAME = "Beamer (other PC).lnk"
DESKTOP, DOWNLOADS, SEND_TO = "Desktop", "Downloads", "SendTo"

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = ctypes.c_void_p
    user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.GetAncestor.restype = ctypes.c_void_p
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
else:
    user32 = None


# User Shell Folders value names, for reading another account's folders from its registry hive.
_SHELL_FOLDER_VALUES = {DESKTOP: "Desktop", DOWNLOADS: "{374DE290-123F-4565-9164-39C4925E467B}", SEND_TO: "SendTo"}


def _sid_of_token() -> str:
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    return win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])


@functools.lru_cache(maxsize=1)
def session_user() -> Optional[tuple]:
    """(SID, profile folder) of whoever is signed in to this session, when that is not the account
    Beamer runs as: a standard user who elevated Beamer with an administrator's credentials. Then
    "Downloads" and "Send to" are that user's, not the administrator's. None otherwise."""
    if user32 is None:
        return None
    try:
        import win32security
        import win32ts
        import winreg

        session = win32ts.WTS_CURRENT_SESSION
        name = win32ts.WTSQuerySessionInformation(None, session, win32ts.WTSUserName)
        domain = win32ts.WTSQuerySessionInformation(None, session, win32ts.WTSDomainName)
        if not name:
            return None
        sid = win32security.ConvertSidToStringSid(win32security.LookupAccountName(domain or None, name)[0])
        if sid == _sid_of_token():
            return None
        key = rf"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\{sid}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as profile:
            path = os.path.expandvars(winreg.QueryValueEx(profile, "ProfileImagePath")[0])
        return sid, path
    except Exception:
        LOGGER.exception("Could not tell who is signed in to this session")
        return None


def _session_user_folder(name: str, user: tuple) -> Optional[str]:
    import winreg

    sid, profile = user
    try:
        with winreg.OpenKey(winreg.HKEY_USERS,
                            rf"{sid}\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            value = str(winreg.QueryValueEx(key, _SHELL_FOLDER_VALUES[name])[0])
    except OSError:
        fallback = {DESKTOP: "Desktop", DOWNLOADS: "Downloads",
                    SEND_TO: os.path.join("AppData", "Roaming", "Microsoft", "Windows", "SendTo")}[name]
        return os.path.join(profile, fallback)
    for variable in ("%USERPROFILE%", "%HOMEPATH%"):
        if variable in value.upper():
            start = value.upper().index(variable)
            value = value[:start] + profile + value[start + len(variable):]
    appdata = os.path.join(profile, "AppData", "Roaming")
    value = value.replace("%APPDATA%", appdata).replace("%appdata%", appdata)
    return os.path.expandvars(value)


def known_folder(name: str) -> str:
    """"Desktop", "Downloads" or "SendTo" for the user signed in to this session, which is the
    account Beamer runs as unless an administrator's credentials elevated it."""
    user = session_user()
    if user is not None:
        folder = _session_user_folder(name, user)
        if folder:
            return folder
    try:
        from win32com.shell import shell, shellcon

        ident = {DESKTOP: shellcon.FOLDERID_Desktop, DOWNLOADS: shellcon.FOLDERID_Downloads,
                 SEND_TO: shellcon.FOLDERID_SendTo}[name]
        return shell.SHGetKnownFolderPath(ident, 0, None)
    except Exception:
        LOGGER.exception("Could not find the %s folder; using the profile's", name)
        if name == SEND_TO:
            return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "SendTo")
        return os.path.join(os.path.expanduser("~"), name)


def received_folder() -> str:
    """Downloads\\Beamer: where sent files go, and dropped ones let go anywhere else."""
    return os.path.join(known_folder(DOWNLOADS), "Beamer")


def _class_of(window) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(window, buffer, 256)
    return buffer.value


def folder_at(x: int, y: int) -> Optional[str]:
    """The file-system folder shown under (x, y): an Explorer window's, or the desktop. None over
    anything else, or for a virtual folder (This PC, a library, a search)."""
    if user32 is None:
        return None
    window = user32.WindowFromPoint(POINT(int(x), int(y)))
    root = user32.GetAncestor(window, GA_ROOT) if window else None
    if not root:
        return None
    kind = _class_of(root)
    if kind in DESKTOP_CLASSES:
        return known_folder(DESKTOP)
    if kind not in EXPLORER_CLASSES:
        return None
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            for shown in win32com.client.Dispatch("Shell.Application").Windows():
                try:
                    if int(shown.HWND) == int(root):
                        path = shown.Document.Folder.Self.Path
                        return path if path and os.path.isdir(path) else None
                except Exception:
                    continue
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        LOGGER.exception("Could not ask Explorer which folder it shows")
    return None


def install_send_to(exe: str) -> None:
    """Explorer's Send to → Beamer (other PC): `Beamer.exe --send <files>`. Rewritten each start,
    so it follows the exe."""
    try:
        import win32com.client

        link = win32com.client.Dispatch("WScript.Shell").CreateShortcut(
            os.path.join(known_folder(SEND_TO), SEND_TO_NAME))
        link.TargetPath = exe
        link.Arguments = "--send"
        link.IconLocation = f"{exe},0"
        link.Description = tr("Send the selected files to the other PC with Beamer")
        link.Save()
    except Exception:
        LOGGER.exception("Could not add Beamer to Send to")


def remove_send_to() -> None:
    try:
        os.remove(os.path.join(known_folder(SEND_TO), SEND_TO_NAME))
    except OSError:
        pass
