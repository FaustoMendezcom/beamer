"""Beamer at the Windows sign-in screen (docs/secure-desktop.md, "Before anyone signs in"):
`BeamerSecure.exe --logon-receiver`, started by the BeamerSecureDesktop service as SYSTEM on the
console session's Winlogon desktop while nobody is signed in there, and ended the moment someone
is, when their own Beamer takes over.

It is the link responder alone: links only from machines already paired with this PC, under the
pairings the installer copied to a folder only SYSTEM and administrators can read; keys and the
pointer, nothing else. No pairing, clipboard, files, settings or layout changes, and it never
writes to disk. The password is typed as at the PC's own keyboard; nothing is stored to sign in."""

import copy
import ctypes
import json
import logging
import os
import sys
import threading
from typing import Optional

LOGGER = logging.getLogger(__name__)
FOLDER_NAME = "logon"
SETTINGS_NAME = "settings.json"
# The identity's caps: none of the optional messages. Input, the pointer's way back and the
# link's own control messages need none.
CAPS: list = []
DPI_AWARENESS_PER_MONITOR_V2 = -4
SIGNED_IN_CHECK_SECONDS = 1.0


def settings_path(exe: Optional[str] = None) -> str:
    """Beside the service exe, under Program Files: <dir>\\logon\\settings.json."""
    folder = os.path.dirname(os.path.abspath(exe or sys.executable))
    return os.path.join(folder, FOLDER_NAME, SETTINGS_NAME)


def installed(exe: Optional[str] = None) -> bool:
    return os.path.isfile(settings_path(exe))


def load_settings(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as handle:
        settings = json.load(handle)
    if not isinstance(settings, dict) or not isinstance(settings.get("peers"), list):
        raise ValueError(f"{path} holds no peers")
    return settings


def identity(settings: dict, version: str) -> dict:
    """This PC as its own Beamer says it, so the paired machines see the same PC."""
    from core import protocol

    machine = protocol.read_id(settings.get("machine_id"))
    if machine is None:
        raise ValueError("the copied settings name no machine id")
    name = str(settings.get("name") or os.environ.get("COMPUTERNAME") or "Windows")[:64]
    return {
        "id": machine,
        "name": name,
        "platform": "windows",
        "app": version[:32] if version.isascii() and version.isprintable() else "dev",
        "caps": list(CAPS),
        "port": int(settings.get("port") or protocol.DEFAULT_PORT),
    }


class MemoryBook:
    """The pairings as read at start, kept in memory: what a link would save (the ids a peer is
    paired with, a dormant mark) changes only this copy, never the file."""

    def __init__(self, settings: dict):
        from core import receiver

        self._settings = copy.deepcopy(settings)
        self.book = receiver.PeerBook(lambda: self._settings, self._keep, threading.RLock())

    def _keep(self, settings: dict) -> None:
        self._settings = copy.deepcopy(settings)


def beacon(own: dict) -> bytes:
    """What the app's pairing service broadcasts with no code up (its name, platform and link port,
    no id), so a paired machine finds this PC again where its address changed with the restart.
    Pairing itself stays shut: no pairing socket listens here."""
    from core import pairing

    return pairing.encode({"type": pairing.MSG_BEACON, "name": own["name"], "port": own["port"],
                           "pairing": pairing.PAIRING_V3, "platform": own["platform"]})


def _announce_forever(own: dict) -> None:
    import socket

    from core import pairing

    message = beacon(own)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    stop = threading.Event()
    while not stop.wait(pairing.BEACON_INTERVAL_SECONDS):
        for target in pairing.broadcast_targets(pairing.PAIRING_PORT):
            try:
                sock.sendto(message, target)
            except OSError:
                continue


def _leave_once_someone_signs_in() -> None:
    """Ends this process the moment a user name appears on its session, whatever the service does:
    that user's own Beamer is about to take the port, and input here would go to a desktop no
    longer shown."""
    import win32ts

    session = ctypes.c_ulong()
    ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    stop = threading.Event()
    while not stop.wait(SIGNED_IN_CHECK_SECONDS):
        try:
            if win32ts.WTSQuerySessionInformation(None, session.value, win32ts.WTSUserName):
                LOGGER.info("Someone signed in to session %s; the sign-in screen receiver ends", session.value)
                logging.shutdown()
                os._exit(0)
        except Exception:
            LOGGER.exception("Could not tell whether anyone signed in")


def _follow_the_input_desktop(input_injector) -> None:
    """Every injection first moves its thread onto the desktop that has the input, as the helper
    does: the process starts on Winlogon's, and early in a start another can be showing. Logs
    each desktop it moves to, so a pointer that does not move says where its input went."""
    import secure_helper

    local = threading.local()
    send = input_injector._send_input

    def follow_then_send(*args, **kwargs):
        desktop = getattr(local, "desktop", None)
        if desktop is None:
            desktop = local.desktop = secure_helper._Desktop()
        name = desktop.follow()
        if name != getattr(local, "name", None):
            local.name = name
            LOGGER.info("Input goes to the %s desktop", name or "unknown")
        return send(*args, **kwargs)

    input_injector._send_input = follow_then_send


def _version() -> str:
    from pathlib import Path

    try:
        root = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent.parent)
        return (root / "VERSION").read_text().strip()
    except OSError:
        return "dev"


def main() -> int:
    from core import protocol
    from core.receiver import LinkResponder

    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_PER_MONITOR_V2))
    except (AttributeError, OSError):
        pass
    path = settings_path()
    try:
        settings = load_settings(path)
        own = identity(settings, _version())
    except (OSError, ValueError):
        LOGGER.exception("No pairings to take input under at the sign-in screen")
        return 2
    import desktop_win
    import input_injector

    _follow_the_input_desktop(input_injector)
    book = MemoryBook(settings).book
    server = LinkResponder(
        book,
        lambda: own,
        status_callback=lambda state, detail: LOGGER.info("Sign-in screen receiver: %s %s", state, detail),
        owner_callback=lambda peer: LOGGER.info("Driven by %s", protocol.id_text(peer) if peer else "nobody"),
        injector=input_injector,
        desktop=desktop_win,
        zones=book.zones,
    )
    # Nobody is signed in, so this is always the secure desktop: keep the owner's input on it.
    server.secure_desktop = lambda: True
    LOGGER.info("Sign-in screen receiver for %s, %d pairings", own["name"], len(settings["peers"]))
    server.start(own["port"])
    threading.Thread(target=_announce_forever, args=(own,), name="Beamer-logon-beacon", daemon=True).start()
    threading.Thread(target=_leave_once_someone_signs_in, name="Beamer-logon-watch", daemon=True).start()
    threading.Event().wait()
    return 0
