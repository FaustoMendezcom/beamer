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


def client_allowed(info: dict, allowed_path: Optional[str], session: int) -> Tuple[bool, str]:
    """Whether a pipe client may drive the secure desktop: in this session, the allowed Beamer.exe
    (as the installer wrote it, under HKLM), and elevated. `info` holds the client's `session`,
    `path` and `elevated`. (allowed, why not)."""
    if info.get("session") != session:
        return False, "another session"
    path = (info.get("path") or "").lower()
    if not allowed_path or path != allowed_path.lower():
        return False, f"not the allowed Beamer ({info.get('path')})"
    if info.get("elevated") is not True:
        return False, "Beamer is not running as administrator"
    return True, ""


class Client:
    """Beamer's end: forwards input for the secure desktop to the helper, one message at a time."""

    def __init__(self, session: Optional[int] = None, opener=None, clock=None):
        import time

        self._session = session
        self._opener = opener or _open_pipe
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._handle = None
        self._failed_at = None
        self.refused = ""

    def _connect(self):
        if self._handle is not None:
            return self._handle
        if self._failed_at is not None and self._clock() - self._failed_at < RETRY_SECONDS:
            return None
        session = self._session if self._session is not None else current_session()
        self._handle = self._opener(pipe_name(session))
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


def _open_pipe(name: str):
    """The helper's pipe, or None when no helper listens (not installed, or between sessions)."""
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
        handle = win32file.CreateFile(name, win32file.GENERIC_READ | win32file.GENERIC_WRITE, 0, None,
                                      win32file.OPEN_EXISTING, 0, None)
        win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_BYTE, None, None)
        return _PipeFile(handle)
    except Exception:
        LOGGER.info("No secure desktop helper at %s", name)
        return None
