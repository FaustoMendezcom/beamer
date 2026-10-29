"""Files on the Windows clipboard: what Explorer copied (CF_HDROP), files received from a peer put
there as a copy for Explorer to paste, and taking them off again when they expire. Mark of the
Web on received files, so Windows treats them as coming from another computer.

Kept apart from clipboard_win, which carries text and pictures across: these never travel on a
link, only their names and sizes do (file_transfer_win)."""

import ctypes
import logging
import struct
import sys
import time
from typing import List, Optional

LOGGER = logging.getLogger(__name__)
CF_HDROP = 15
DROPEFFECT_COPY = 1
OPEN_TRIES = 10
OPEN_PAUSE_SECONDS = 0.05
ZONE_INTERNET = 3

if sys.platform == "win32":
    import win32clipboard

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetClipboardSequenceNumber.restype = ctypes.c_ulong
    PREFERRED_DROP_EFFECT = win32clipboard.RegisterClipboardFormat("Preferred DropEffect")
else:
    win32clipboard = user32 = None
    PREFERRED_DROP_EFFECT = 0


def sequence() -> int:
    return int(user32.GetClipboardSequenceNumber()) if user32 is not None else 0


def _open() -> bool:
    for _ in range(OPEN_TRIES):
        try:
            win32clipboard.OpenClipboard(None)
            return True
        except Exception:
            time.sleep(OPEN_PAUSE_SECONDS)
    return False


def read_files() -> Optional[List[str]]:
    """The paths Explorer (or any app) put on the clipboard, or None when it holds no files."""
    if win32clipboard is None or not win32clipboard.IsClipboardFormatAvailable(CF_HDROP):
        return None
    if not _open():
        return None
    try:
        if not win32clipboard.IsClipboardFormatAvailable(CF_HDROP):
            return None
        paths = win32clipboard.GetClipboardData(CF_HDROP)
        return [str(path) for path in paths] or None
    except Exception:
        LOGGER.exception("Could not read the files on the clipboard")
        return None
    finally:
        win32clipboard.CloseClipboard()


def drop_files(paths: List[str]) -> bytes:
    """A DROPFILES block (shlobj.h) naming `paths`, wide, each NUL-terminated, then a final NUL."""
    names = "".join(f"{path}\0" for path in paths) + "\0"
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + names.encode("utf-16-le")


def write_files(paths: List[str]) -> Optional[int]:
    """Puts `paths` on the clipboard as files to copy. The clipboard's sequence number afterwards,
    which says whether it still holds them later, or None when it could not be opened."""
    if win32clipboard is None or not _open():
        return None
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(CF_HDROP, drop_files(paths))
        win32clipboard.SetClipboardData(PREFERRED_DROP_EFFECT, struct.pack("<I", DROPEFFECT_COPY))
    except Exception:
        LOGGER.exception("Could not put the received files on the clipboard")
        return None
    finally:
        win32clipboard.CloseClipboard()
    return sequence()


def clear_if(expected: int) -> bool:
    """Empties the clipboard only while it still holds what was written at sequence `expected`:
    anything copied since is the user's and stays."""
    if win32clipboard is None or sequence() != expected or not _open():
        return False
    try:
        if sequence() != expected:
            return False
        win32clipboard.EmptyClipboard()
        return True
    finally:
        win32clipboard.CloseClipboard()


def mark_from_another_computer(path: str) -> None:
    """Mark of the Web: SmartScreen, Office's Protected View and the "unblock" checkbox apply to a
    received file as to a download. Not possible on FAT drives; never fatal."""
    try:
        with open(path + ":Zone.Identifier", "w", encoding="ascii", newline="") as stream:
            stream.write(f"[ZoneTransfer]\r\nZoneId={ZONE_INTERNET}\r\n")
    except OSError:
        pass
