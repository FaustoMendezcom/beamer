"""The secure desktop helper (docs/secure-desktop.md): `BeamerSecure.exe --secure-helper`, started
as SYSTEM in the signed-in user's session by the BeamerSecureDesktop service.

It serves `\\\\.\\pipe\\BeamerSecureInput-<session>` to one client at a time, and only to the
allowed, elevated Beamer of this session (secure_input.client_allowed). For each message it
attaches to whichever desktop has the input now (Default, or Winlogon for UAC, the lock screen and
Ctrl+Alt+Del) and sends the input there, or Ctrl+Alt+Del itself. It runs nothing it is sent."""

import ctypes
import logging
import sys
from typing import Callable, Optional

import secure_input

LOGGER = logging.getLogger(__name__)
PIPE_BUFFER = 64 * 1024
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
# One serving, one waiting, so the name stays held while a client is dealt with.
PIPE_INSTANCES = 2
# SYSTEM full; interactive users only read and write data (FILE_GENERIC_READ | FILE_WRITE_DATA),
# never FILE_CREATE_PIPE_INSTANCE, which GENERIC_WRITE would include and which would let any
# program add its own instance of this pipe. Who may actually use it is checked per client.
PIPE_SDDL = "D:P(A;;GA;;;SY)(A;;0x0012008B;;;IU)"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
GENERIC_ALL = 0x10000000
UOI_NAME = 2


class Helper:
    """The message handling, apart from pipes and Win32 so it can be tested."""

    def __init__(self, send_input: Callable[[bytes, int], int], sas: Callable[[], bool],
                 desktop: Callable[[], str], record_size: int):
        self._send_input, self._sas, self._desktop, self._size = send_input, sas, desktop, record_size

    def handle(self, kind: int, payload: bytes) -> bytes:
        try:
            if kind == secure_input.INPUTS:
                records = secure_input.read_inputs(payload, self._size)
                count = len(records) // self._size
                sent = self._send_input(records, count)
                return secure_input.answer(secure_input.OK if sent == count else secure_input.FAILED,
                                           "" if sent == count else f"{sent} of {count} sent")
            if kind == secure_input.SAS:
                return secure_input.answer(secure_input.OK if self._sas() else secure_input.FAILED)
            return secure_input.answer(secure_input.OK, self._desktop())
        except secure_input.FrameError as exc:
            return secure_input.answer(secure_input.FAILED, str(exc))


# -- Win32 ---------------------------------------------------------------------------------------

def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.OpenInputDesktop.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    user32.SetThreadDesktop.argtypes = [ctypes.c_void_p]
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    user32.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
                                                 ctypes.POINTER(ctypes.c_ulong)]
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
    return user32


class _Desktop:
    """Keeps this thread on the desktop that has the input, as SendInput reaches only that one."""

    def __init__(self):
        self._user32 = _user32()
        self._current = None

    def follow(self) -> str:
        desktop = self._user32.OpenInputDesktop(0, False, GENERIC_ALL)
        if not desktop:
            return ""
        name = ctypes.create_unicode_buffer(64)
        needed = ctypes.c_ulong()
        self._user32.GetUserObjectInformationW(desktop, UOI_NAME, name, ctypes.sizeof(name), ctypes.byref(needed))
        if self._user32.SetThreadDesktop(desktop):
            if self._current:
                self._user32.CloseDesktop(self._current)
            self._current = desktop
        else:
            self._user32.CloseDesktop(desktop)
        return name.value

    def send(self, records: bytes, count: int) -> int:
        self.follow()
        buffer = ctypes.create_string_buffer(records, len(records))
        return int(self._user32.SendInput(count, buffer, len(records) // count))


def _sas() -> bool:
    """Asks the service for Ctrl+Alt+Del: Windows takes SendSAS only from a service."""
    import pywintypes
    import win32event

    import secure_service

    try:
        event = win32event.OpenEvent(win32event.EVENT_MODIFY_STATE, False, secure_service.SAS_EVENT)
    except pywintypes.error:
        LOGGER.exception("Could not reach the service for Ctrl+Alt+Del")
        return False
    try:
        win32event.SetEvent(event)
        LOGGER.info("Asked the service for Ctrl+Alt+Del")
        return True
    finally:
        event.Close()


def _client_info(pipe) -> dict:
    """Session, image path and elevation of the process at the other end of `pipe`."""
    import win32api
    import win32pipe
    import win32security

    pid = win32pipe.GetNamedPipeClientProcessId(pipe)
    session = ctypes.c_ulong()
    ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(session))
    process = win32api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    try:
        size = ctypes.c_ulong(1024)
        path = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(int(process), 0, path, ctypes.byref(size))
        token = win32security.OpenProcessToken(process, TOKEN_QUERY)
        elevated = bool(win32security.GetTokenInformation(token, TOKEN_ELEVATION))
    finally:
        win32api.CloseHandle(process)
    return {"pid": pid, "session": int(session.value), "path": path.value, "elevated": elevated}


def allowed_client() -> Optional[str]:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, secure_input.REGISTRY_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, secure_input.ALLOWED_CLIENT_VALUE)
            return str(value)
    except OSError:
        return None


class PipeTaken(OSError):
    """Another program already holds this pipe name: it may be posing as the helper."""


def serve_forever(session: Optional[int] = None) -> None:
    import input_injector
    import pywintypes
    import win32file
    import win32pipe
    import win32security

    session = secure_input.current_session() if session is None else session
    desktop = _Desktop()
    helper = Helper(desktop.send, _sas, desktop.follow, ctypes.sizeof(input_injector.INPUT))
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        PIPE_SDDL, win32security.SDDL_REVISION_1)
    name = secure_input.pipe_name(session)

    def instance(first: bool):
        flags = win32pipe.PIPE_ACCESS_DUPLEX | (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0)
        try:
            return win32pipe.CreateNamedPipe(
                name, flags,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                PIPE_INSTANCES, PIPE_BUFFER, PIPE_BUFFER, 0, attributes)
        except pywintypes.error as exc:
            raise PipeTaken(f"{name} could not be created ({exc.strerror})") from None

    # The first instance claims the name: if any other program made it first, this fails rather
    # than share it. From then on the next instance is made before the current one closes, so the
    # name is never free for another program to take between clients.
    pipe = instance(first=True)
    LOGGER.info("Secure desktop helper serving %s", name)
    while True:
        spare = None
        try:
            win32pipe.ConnectNamedPipe(pipe, None)
            spare = instance(first=False)
            info = _client_info(pipe)
            allowed, why = secure_input.client_allowed(info, allowed_client(), session)
            if not allowed:
                LOGGER.warning("Refused %s (pid %s): %s", info.get("path"), info.get("pid"), why)
                _read_message(pipe)
                win32file.WriteFile(pipe, secure_input.answer(secure_input.REFUSED, why))
            else:
                while True:
                    kind, payload = _read_message(pipe)
                    win32file.WriteFile(pipe, helper.handle(kind, payload))
        except PipeTaken:
            raise
        except (pywintypes.error, secure_input.FrameError, OSError) as exc:
            LOGGER.info("Secure desktop client gone: %s", exc)
        finally:
            try:
                # Unread answers are discarded by a disconnect; let the client have them first.
                win32file.FlushFileBuffers(pipe)
                win32pipe.DisconnectNamedPipe(pipe)
            except pywintypes.error:
                pass
            win32file.CloseHandle(pipe)
            # In the finally, so a refusal or a gone client still moves on to the waiting instance.
            pipe = spare if spare is not None else instance(first=False)


def _read_message(pipe):
    import win32file

    def read(count):
        data = b""
        while len(data) < count:
            _result, chunk = win32file.ReadFile(pipe, count - len(data))
            if not chunk:
                raise OSError("pipe closed")
            data += chunk
        return data

    kind, length = secure_input.read_header(read(secure_input.HEADER.size))
    return kind, read(length) if length else b""


def main() -> int:
    if sys.platform != "win32":
        return 2
    try:
        serve_forever()
    except PipeTaken as exc:
        LOGGER.error("Not serving: %s. Another program may be posing as the helper; Beamer will not use it.", exc)
        return 3
    except Exception:
        LOGGER.exception("The secure desktop helper stopped")
        return 1
    return 0
