"""Input for the Windows secure desktop (docs/secure-desktop.md): the pipe between Beamer and the
SYSTEM helper that types and clicks where Beamer may not (UAC prompts, the lock screen, the Ctrl+Alt+
Del screen), and the rule for who may use it.

Messages are `BSI1`, a kind byte and a little-endian payload length, then the payload:

- `INPUTS`: a u32 record size, then `INPUT` records as SendInput takes them;
- `SAS`: nothing; the helper sends Ctrl+Alt+Del (SendSAS);
- `PING`: nothing; answers the name of the desktop that has the input.

Each answer is one status byte (`OK`, `REFUSED`, `FAILED`) and up to 255 bytes of UTF-8 text."""

import ctypes
import logging
import struct
import sys
import threading
from typing import Optional, Tuple

LOGGER = logging.getLogger(__name__)
MAGIC = b"BSI1"
HEADER = struct.Struct("<4sBI")
INPUTS, SAS, PING = 1, 2, 3
KINDS = (INPUTS, SAS, PING)
OK, REFUSED, FAILED = 0, 1, 2
MAX_PAYLOAD = 64 * 1024
PIPE_PREFIX = r"\\.\pipe\BeamerSecureInput-"
REGISTRY_KEY = r"SOFTWARE\Beamer\SecureDesktop"
ALLOWED_CLIENT_VALUE = "AllowedClient"
SERVICE_EXE_VALUE = "ServiceExe"
# The SID of a standard account whose unelevated Beamer may use the helper (client_allowed).
STANDARD_USER_VALUE = "StandardUser"
ADMINISTRATORS_SID = "S-1-5-32-544"
SYSTEM_SID = "S-1-5-18"
# CreateFile: the server may identify this client but never act as it (no impersonation).
SECURITY_SQOS_PRESENT = 0x00100000
SECURITY_IDENTIFICATION = 0x00010000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
FILE_WRITE_DATA = 0x0002
TOKEN_QUERY = 0x0008
CONNECT_TIMEOUT_MS = 200
# A refused or missing helper is asked again only after this, so input on the secure desktop does
# not wait on a pipe that is not there for every key.
RETRY_SECONDS = 5.0


class FrameError(ValueError):
    pass


def pipe_name(session: int) -> str:
    return f"{PIPE_PREFIX}{int(session)}"


def frame(kind: int, payload: bytes = b"") -> bytes:
    if kind not in KINDS or len(payload) > MAX_PAYLOAD:
        raise FrameError("not a secure input message")
    return HEADER.pack(MAGIC, kind, len(payload)) + payload


def read_header(data: bytes) -> Tuple[int, int]:
    """(kind, payload length) from a message's first HEADER.size bytes, or FrameError."""
    if len(data) != HEADER.size:
        raise FrameError("short header")
    magic, kind, length = HEADER.unpack(data)
    if magic != MAGIC or kind not in KINDS or length > MAX_PAYLOAD:
        raise FrameError("not a secure input message")
    if kind != INPUTS and length:
        raise FrameError("this message carries nothing")
    return kind, length


def inputs_payload(records: bytes, size: int) -> bytes:
    return struct.pack("<I", size) + records


def read_inputs(payload: bytes, size: int) -> bytes:
    """The INPUT records from an INPUTS payload, held to whole records of this build's size."""
    if len(payload) < 4:
        raise FrameError("no record size")
    (claimed,) = struct.unpack("<I", payload[:4])
    records = payload[4:]
    if claimed != size or not records or len(records) % size:
        raise FrameError("records of another size")
    return records


def answer(status: int, text: str = "") -> bytes:
    data = text.encode("utf-8")[:255]
    return bytes([status, len(data)]) + data


def client_allowed(info: dict, allowed_path: Optional[str], session: int,
                   standard_user: Optional[str] = None) -> Tuple[bool, str]:
    """Whether a pipe client may drive the secure desktop: in this session, the allowed Beamer.exe
    (as the installer wrote it, under HKLM), and elevated. Or, where the installer named a standard
    account (`standard_user`, its SID), that account's own unelevated Beamer: a standard user's UAC
    prompt always asks for an administrator's password, so nothing typed there approves itself.
    An administrator's unelevated Beamer never counts, as its UAC prompts are one Yes click.
    `info` holds the client's `session`, `path`, `elevated`, `sid` and `administrator`.
    (allowed, why not)."""
    if info.get("session") != session:
        return False, "another session"
    path = (info.get("path") or "").lower()
    if not allowed_path or path != allowed_path.lower():
        return False, f"not the allowed Beamer ({info.get('path')})"
    if info.get("elevated") is True:
        return True, ""
    if standard_user and info.get("sid") == standard_user and info.get("administrator") is False:
        return True, ""
    return False, "Beamer is not running as administrator"


def server_trusted(info: dict, expected_path: Optional[str], session: int) -> Tuple[bool, str]:
    """Whether the process serving the pipe is the installed helper: SYSTEM, the service's own exe
    under Program Files (as the installer wrote it, under HKLM), in this session. Checked before a
    single byte is written, as any program could create a pipe of that name first and read the
    passwords typed on the secure desktop. (trusted, why not).

    A standard account's Beamer may not open a SYSTEM process at all (`opened` False): then the
    pipe's own owner stands for it, which must be SYSTEM. No program of a standard or unelevated
    account can make an object SYSTEM owns, only SYSTEM itself, whose every process is trusted."""
    if info.get("opened") is False:
        if info.get("owner") != SYSTEM_SID:
            return False, f"the pipe is owned by {info.get('owner') or 'an unknown account'}, not SYSTEM"
        if info.get("session") != session:
            return False, "the pipe is served from another session"
        return True, ""
    if info.get("sid") != SYSTEM_SID:
        return False, f"the pipe is served by {info.get('sid') or 'an unknown account'}, not SYSTEM"
    path = (info.get("path") or "").lower()
    if not expected_path or path != expected_path.lower():
        return False, f"the pipe is served by {info.get('path') or 'an unknown program'}"
    if info.get("session") != session:
        return False, "the pipe is served from another session"
    return True, ""


def installed() -> Optional[Tuple[str, str]]:
    """(allowed client, service exe) from the key only an administrator can write, when the secure
    desktop service is installed; else None, and Beamer never opens the pipe at all."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_KEY) as key:
            client = str(winreg.QueryValueEx(key, ALLOWED_CLIENT_VALUE)[0])
            service = str(winreg.QueryValueEx(key, SERVICE_EXE_VALUE)[0])
    except OSError:
        return None
    return (client, service) if client and service else None


class Client:
    """Beamer's end: forwards input for the secure desktop to the helper, one message at a time,
    only to a helper it has checked (server_trusted)."""

    def __init__(self, session: Optional[int] = None, opener=None, clock=None, installation=None):
        import time

        self._session = session
        self._opener = opener or _open_pipe
        self._installation = installation or installed
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._handle = None
        self._failed_at = None
        self._checked = None   # (when, whether the helper answered a ping)
        self.refused = ""

    def _connect(self):
        if self._handle is not None:
            return self._handle
        if self._failed_at is not None and self._clock() - self._failed_at < RETRY_SECONDS:
            return None
        found = self._installation()
        if found is None:
            # Not installed: no pipe is opened, so no program can stand in for the helper.
            self._failed_at = self._clock()
            return None
        session = self._session if self._session is not None else current_session()
        self._handle = self._opener(pipe_name(session), found[1], session)
        if self._handle is None:
            self._failed_at = self._clock()
        return self._handle

    def request(self, kind: int, payload: bytes = b"") -> Tuple[int, str]:
        """(status, text); (FAILED, why) when there is no helper or the pipe broke."""
        with self._lock:
            handle = self._connect()
            if handle is None:
                return FAILED, "no secure desktop helper"
            try:
                handle.write(frame(kind, payload))
                head = handle.read(2)
                status, length = head[0], head[1]
                text = handle.read(length).decode("utf-8", "replace") if length else ""
            except Exception as exc:  # OSError, or pywintypes.error, which is not one
                self._close_locked()
                self._failed_at = self._clock()
                return FAILED, str(exc)
            if status == REFUSED:
                if text != self.refused:
                    LOGGER.warning("The secure desktop helper refused Beamer: %s", text)
                self.refused = text
                self._close_locked()
                self._failed_at = self._clock()
            return status, text

    def available(self) -> bool:
        """Whether a helper is installed and lets this Beamer use it, asked at most every
        RETRY_SECONDS: the receiver keeps a locked machine's input when it is."""
        if self._checked is not None and self._clock() - self._checked[0] < RETRY_SECONDS:
            return self._checked[1]
        answered = self.request(PING)[0] == OK
        self._checked = (self._clock(), answered)
        return answered

    def send_inputs(self, records: bytes, size: int) -> bool:
        return self.request(INPUTS, inputs_payload(records, size))[0] == OK

    def sas(self) -> bool:
        return self.request(SAS)[0] == OK

    def _close_locked(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def current_session() -> int:
    if sys.platform != "win32":
        return 0
    session = ctypes.c_ulong()
    ctypes.windll.kernel32.ProcessIdToSessionId(ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(session))
    return int(session.value)


class _PipeFile:
    def __init__(self, handle):
        self._handle = handle

    def write(self, data: bytes) -> None:
        import win32file

        win32file.WriteFile(self._handle, data)

    def read(self, count: int) -> bytes:
        import win32file

        data = b""
        while len(data) < count:
            _result, chunk = win32file.ReadFile(self._handle, count - len(data))
            if not chunk:
                raise OSError("the secure desktop helper closed the pipe")
            data += chunk
        return data

    def close(self) -> None:
        import win32file

        win32file.CloseHandle(self._handle)


def server_info(handle) -> dict:
    """Account, image path and session of the process serving pipe `handle`."""
    import win32api
    import win32pipe
    import win32security

    import pywintypes

    pid = win32pipe.GetNamedPipeServerProcessId(handle)
    session = ctypes.c_ulong()
    if not ctypes.windll.kernel32.GetNamedPipeServerSessionId(int(handle), ctypes.byref(session)):
        raise OSError("the pipe's session could not be read")
    descriptor = win32security.GetSecurityInfo(int(handle), win32security.SE_KERNEL_OBJECT,
                                               win32security.OWNER_SECURITY_INFORMATION)
    owner = win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
    info = {"pid": pid, "session": int(session.value), "path": None, "sid": None, "owner": owner, "opened": True}
    try:
        process = win32api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except pywintypes.error as exc:
        if exc.winerror != ERROR_ACCESS_DENIED:
            raise
        # A standard account may not open a SYSTEM process: the pipe's owner tells instead.
        info["opened"] = False
        return info
    try:
        size = ctypes.c_ulong(1024)
        path = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(int(process), 0, path, ctypes.byref(size)):
            info["path"] = path.value
        token = win32security.OpenProcessToken(process, TOKEN_QUERY)
        sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
        info["sid"] = win32security.ConvertSidToStringSid(sid)
    finally:
        win32api.CloseHandle(process)
    return info


def _open_pipe(name: str, expected_path: str, session: int):
    """The helper's pipe, once its server is checked, or None: no helper listening, or a server
    that is not the installed helper (nothing is written to that one)."""
    if sys.platform != "win32":
        return None
    try:
        import pywintypes
        import win32file
        import win32pipe

        try:
            win32pipe.WaitNamedPipe(name, CONNECT_TIMEOUT_MS)
        except pywintypes.error:
            return None
        handle = win32file.CreateFile(name, win32file.GENERIC_READ | FILE_WRITE_DATA, 0, None,
                                      win32file.OPEN_EXISTING, SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                                      None)
    except Exception:
        LOGGER.info("No secure desktop helper at %s", name)
        return None
    try:
        info = server_info(handle)
        trusted, why = server_trusted(info, expected_path, session)
    except Exception as exc:
        trusted, why = False, f"its server could not be checked ({exc})"
    if not trusted:
        LOGGER.warning("Not using %s: %s", name, why)
        win32file.CloseHandle(handle)
        return None
    try:
        win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_BYTE, None, None)
    except Exception:
        pass
    return _PipeFile(handle)
