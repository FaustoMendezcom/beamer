"""Files between machines (WIRE.md section 10, files): the manifest of what was copied, the data
connection that carries it, and the checks every name from a peer passes before it is written.

The files never travel on a link: input would wait behind them. The recipient opens a data
connection to the sender's port with the link's own handshake, sends `files_fetch` in place of
`hello`, and reads back, each frame sealed as a link's are:

- the manifest, one JSON frame: `{"type":"files_manifest","data":{"items":[...]}}`, or
  `{"type":"files_refused","data":{"reason":...}}`;
- for each file in manifest order, data frames `00 <bytes>` and an end frame `01`;
- a last frame `02` once every file has gone.

Pure but for sockets and the file system; the platform's clipboard and Mark of the Web are the
caller's."""

import os
import re
import socket
import stat
import threading
import time
from typing import Callable, List, Optional, Sequence

from core import protocol

CHUNK_BYTES = 1024 * 1024
SOCKET_BUFFER_BYTES = 4 * 1024 * 1024
READ_TIMEOUT_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 5.0
HANDSHAKE_SECONDS = 10.0
# What a paste carries; larger sets are refused up front rather than half staged.
PASTE_MAX_BYTES = 2 * 1024 ** 3
MAX_ITEMS = 20000
MAX_DEPTH = 32
MAX_NAME_CHARS = 255
MAX_PATH_CHARS = 1024

DATA, END_OF_FILE, DONE = b"\x00", b"\x01", b"\x02"
# Device names Windows opens whatever the extension, superscript digits included (COM\u00b9), and
# the console's own.
_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$",
             *(f"{device}{digit}" for device in ("com", "lpt") for digit in "123456789\u00b9\u00b2\u00b3")}
# Control characters, and the bidirectional overrides and marks that make "gpj.exe" read as "exe.jpg".
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]')
# A file time any file system takes: 1970 to 9999.
MTIME_MAX = 253402300799


class TransferError(Exception):
    """The set could not be sent or received; `text` says why."""


class TooLarge(TransferError):
    pass


# -- the manifest -------------------------------------------------------------------------------

def safe_part(part: str) -> bool:
    """Whether one name from a peer can be written as it is on Windows and anywhere else."""
    return (isinstance(part, str) and 0 < len(part) <= MAX_NAME_CHARS and part not in (".", "..")
            and not _BAD_CHARS.search(part) and not part.endswith((".", " "))
            and part.split(".")[0].rstrip(" ").lower() not in _RESERVED)


def safe_path(path: str) -> Optional[List[str]]:
    """The parts of a manifest path ("folder/file.txt"), or None for anything that could leave the
    folder it is written in: absolute, a drive, `..`, a stream (`:`), a reserved device name."""
    if not isinstance(path, str) or not 0 < len(path) <= MAX_PATH_CHARS:
        return None
    parts = path.split("/")
    if len(parts) > MAX_DEPTH or not all(safe_part(part) for part in parts):
        return None
    return parts


def _unique(name: str, taken: set) -> str:
    stem, dot, ext = name.rpartition(".") if "." in name[1:] else (name, "", "")
    candidate, number = name, 2
    while candidate.lower() in taken:
        candidate = f"{stem} ({number}){dot}{ext}"
        number += 1
    taken.add(candidate.lower())
    return candidate


def build_manifest(paths: Sequence[str], max_bytes: int = PASTE_MAX_BYTES,
                   readable: Callable[[str], bool] = lambda path: True):
    """(items, sources, total) for files and folders copied on this machine: `items` as the
    manifest sends them ({path, size, mtime, dir}), `sources` the local path of each (None for a
    folder). Links and other reparse points are left out, so nothing outside what was copied is
    sent, nor anything `readable` refuses (an elevated sender reads only what its user could).
    Raises TooLarge past `max_bytes` or MAX_ITEMS."""
    items, sources, total = [], [], 0
    taken = set()

    def add(local: str, relative: str, info: os.stat_result, folder: bool) -> None:
        nonlocal total
        if len(items) >= MAX_ITEMS:
            raise TooLarge(f"more than {MAX_ITEMS} files and folders")
        size = 0 if folder else info.st_size
        total += size
        if total > max_bytes:
            raise TooLarge(f"more than {max_bytes // 1024 ** 2} MB")
        items.append({"path": relative, "size": size, "mtime": int(info.st_mtime), "dir": folder})
        sources.append(None if folder else local)

    def walk(local: str, relative: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise TooLarge("folders nested too deep")
        with os.scandir(local) as entries:
            for entry in sorted(entries, key=lambda item: item.name.lower()):
                if not safe_part(entry.name) or entry.is_symlink() or _is_reparse(entry) or not readable(entry.path):
                    continue
                child = f"{relative}/{entry.name}"
                if entry.is_dir(follow_symlinks=False):
                    add(entry.path, child, entry.stat(follow_symlinks=False), True)
                    walk(entry.path, child, depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    add(entry.path, child, entry.stat(follow_symlinks=False), False)

    for path in paths:
        local = os.path.abspath(path)
        try:
            info = os.lstat(local)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or _reparse(info) or not readable(local):
            continue
        name = os.path.basename(local.rstrip("\\/")) or "item"
        if not safe_part(name):
            continue
        top = _unique(name, taken)
        if stat.S_ISDIR(info.st_mode):
            add(local, top, info, True)
            walk(local, top, 1)
        elif stat.S_ISREG(info.st_mode):
            add(local, top, info, False)
    return items, sources, total


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _is_reparse(entry: os.DirEntry) -> bool:
    try:
        return _reparse(entry.stat(follow_symlinks=False))
    except OSError:
        return True


def read_manifest(items, max_bytes: int = PASTE_MAX_BYTES):
    """The manifest as received, checked: every path safe, sizes whole, within the limits, each
    file's folder listed before it. Raises TransferError."""
    if not isinstance(items, list) or not 0 < len(items) <= MAX_ITEMS:
        raise TransferError("the list of files is empty or too long")
    folders, seen, total, checked = set(), set(), 0, []
    for item in items:
        if not isinstance(item, dict):
            raise TransferError("a file entry is malformed")
        parts = safe_path(item.get("path"))
        size, mtime, folder = item.get("size"), item.get("mtime"), item.get("dir")
        if (parts is None or type(folder) is not bool or type(size) is not int or size < 0
                or type(mtime) is not int or not 0 <= mtime <= MTIME_MAX or (folder and size)):
            raise TransferError("a file entry is malformed or names a path outside the transfer")
        key = "/".join(parts).lower()
        if key in seen or (len(parts) > 1 and "/".join(parts[:-1]).lower() not in folders):
            raise TransferError("a file entry repeats or is outside its folder")
        seen.add(key)
        if folder:
            folders.add(key)
        total += size
        if total > max_bytes:
            raise TooLarge("the files are larger than allowed")
        checked.append({"parts": parts, "size": size, "mtime": mtime, "dir": folder})
    return checked, total


# -- the data connection ------------------------------------------------------------------------

def _tune(sock: socket.socket) -> None:
    for option in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, option, SOCKET_BUFFER_BYTES)
        except OSError:
            pass


def _send_raw(sock: socket.socket, session, body: bytes) -> None:
    with session.send_lock:
        sock.sendall(session.seal_raw(body))


def _recv_raw(sock: socket.socket, session) -> bytes:
    return session.open_raw(protocol.recv_frame(sock, protocol.MAX_FRAME_BYTES))


def serve(sock: socket.socket, session, items, sources, cancelled: Optional[threading.Event] = None,
          progress: Optional[Callable[[int], None]] = None) -> None:
    """Sends an accepted fetch: the manifest, then every file. Run on the data connection's own
    thread; raises OSError or TransferError when the connection or a file fails."""
    sock.settimeout(READ_TIMEOUT_SECONDS)
    _tune(sock)
    _send_raw(sock, session, protocol.dumps_message({"type": "files_manifest", "data": {"items": items}}))
    buffer = bytearray(CHUNK_BYTES)
    view = memoryview(buffer)
    sent = 0
    for item, source in zip(items, sources):
        if item["dir"]:
            continue
        with open(source, "rb") as handle:
            remaining = item["size"]
            while remaining > 0:
                if cancelled is not None and cancelled.is_set():
                    raise TransferError("cancelled")
                count = handle.readinto(view[:min(CHUNK_BYTES, remaining)])
                if not count:
                    raise TransferError(f"{item['path']} got shorter while it was being sent")
                _send_raw(sock, session, DATA + bytes(view[:count]))
                remaining -= count
                sent += count
                if progress is not None:
                    progress(sent)
        _send_raw(sock, session, END_OF_FILE)
    _send_raw(sock, session, DONE)


def refuse(sock: socket.socket, session, reason: str) -> None:
    try:
        _send_raw(sock, session, protocol.dumps_message({"type": "files_refused", "data": {"reason": reason}}))
    except OSError:
        pass


def fetch(host: str, port: int, token: str, own_id: bytes, offer: bytes, destination: str,
          cancelled: Optional[threading.Event] = None, progress: Optional[Callable[[int, int], None]] = None,
          written: Optional[Callable[[str], None]] = None, max_bytes: int = PASTE_MAX_BYTES,
          connect: Callable = socket.create_connection) -> List[str]:
    """Fetches offer `offer` from the machine at `host`:`port` into `destination`, a new empty
    folder, and returns the paths of the top-level items written there. `written(path)` is called
    for each file once it is complete (the caller marks it). Raises TransferError or OSError."""
    sock = connect((host, port), CONNECT_TIMEOUT_SECONDS)
    try:
        _tune(sock)
        session = protocol.LinkSession(token, protocol.ROLE_INITIATOR)
        deadline = time.monotonic() + HANDSHAKE_SECONDS
        sock.settimeout(HANDSHAKE_SECONDS)
        sock.sendall(session.preamble())
        session.accept_preamble(protocol.recv_link_preamble(sock, deadline))
        protocol.send_msg(sock, session, protocol.files_fetch_msg(offer, own_id))
        sock.settimeout(READ_TIMEOUT_SECONDS)
        head = protocol.parse_message(_recv_raw(sock, session))
        if head.get("type") == "files_refused":
            reason = head.get("data", {}).get("reason") if isinstance(head.get("data"), dict) else None
            raise TransferError(str(reason or "the other machine refused the files"))
        if head.get("type") != "files_manifest" or not isinstance(head.get("data"), dict):
            raise TransferError("the other machine answered with something other than a list of files")
        checked, total = read_manifest(head["data"].get("items"), max_bytes)
        return _receive(sock, session, checked, total, destination, cancelled, progress, written)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _receive(sock, session, checked, total, destination, cancelled, progress, written) -> List[str]:
    root = os.path.realpath(destination)
    tops: List[str] = []
    received = 0
    for item in checked:
        target = os.path.join(root, *item["parts"])
        if os.path.commonpath([root, os.path.realpath(target)]) != root:
            raise TransferError("a file would be written outside the transfer folder")
        if len(item["parts"]) == 1:
            tops.append(target)
        if item["dir"]:
            os.makedirs(target, exist_ok=True)
            continue
        with open(target, "xb") as handle:
            remaining = item["size"]
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise TransferError("cancelled")
                frame = _recv_raw(sock, session)
                kind, body = frame[:1], frame[1:]
                if kind == END_OF_FILE:
                    break
                if kind != DATA or len(body) > remaining:
                    raise TransferError("the file data did not match the list of files")
                handle.write(body)
                remaining -= len(body)
                received += len(body)
                if progress is not None:
                    progress(received, total)
            if remaining:
                raise TransferError("a file arrived shorter than listed")
        os.utime(target, (item["mtime"], item["mtime"]))
        if written is not None:
            written(target)
    for item in checked:
        if item["dir"]:
            target = os.path.join(root, *item["parts"])
            os.utime(target, (item["mtime"], item["mtime"]))
    if _recv_raw(sock, session) != DONE:
        raise TransferError("the transfer did not end cleanly")
    return tops

