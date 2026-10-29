"""Files copied on one PC, pasted on another (core.file_transfer for the wire, WIRE.md section 10).

Copying files in Explorer offers them, by name and size only, to every linked peer that takes
`files`. A peer fetches the latest offer when input arrives on it, into a staging folder, and puts
the files on its clipboard for Explorer to paste. They stay pasteable for `paste_seconds` (two
minutes by default): after that they leave the clipboard, if it still holds them, so an old copy
is never pasted by surprise, and the staging folder goes a while later, once any paste has had
time to finish. Nothing is carried until it is about to be pasted, and the data connection lasts
only as long as the transfer."""

import logging
import os
import shutil
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from core import file_transfer, protocol
from core.i18n import tr

LOGGER = logging.getLogger(__name__)
WATCH_SECONDS = 0.5
# An offer is fetched only this soon after it was made: input arriving an hour later is not a paste.
OFFER_SECONDS = 600
PASTE_SECONDS = 120
# After the paste window closes the staging folder stays this long, for a paste still copying.
DELETE_AFTER_SECONDS = 1800
PURGE_OLDER_SECONDS = 86400
LARGE_BYTES = 50 * 1024 * 1024
# Dropped and sent files go to a folder, not the clipboard, so far more may go: up to this, and the
# free space there.
SEND_MAX_BYTES = 1024 ** 4
# A dropped set waits this long for the button to be let go before it goes to Downloads\Beamer.
DROP_WAIT_SECONDS = 20
FETCHES_AT_ONCE = 2


def _size(total: int) -> str:
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if total >= scale:
            return f"{total / scale:.1f} {unit}"
    return f"{total} bytes"


def label_for(items: List[dict]) -> str:
    tops = [item["path"] for item in items if "/" not in item["path"]]
    files = sum(1 for item in items if not item["dir"])
    if len(tops) == 1:
        return tops[0]
    return tr("{count} items").format(count=len(tops)) if tops else tr("{count} files").format(count=files)


class FileShare:
    def __init__(self, clipboard, *, own_id: bytes, staging: str,
                 send: Callable[[bytes, dict], None], targets: Callable[[], List[bytes]],
                 peer: Callable[[bytes], Optional[Tuple[str, int, str, str]]],
                 notify: Callable[[str], None], paste_seconds: int = PASTE_SECONDS,
                 fetch=file_transfer.fetch, max_bytes: int = file_transfer.PASTE_MAX_BYTES,
                 received: Callable[[], str] = lambda: "", dropped: Optional[Callable[[dict], None]] = None,
                 opened: Optional[Callable[[str], None]] = None,
                 may_read: Callable[[str], bool] = lambda path: True):
        """`clipboard` is clipboard_files_win (or a fake); `send(peer, message)` posts on a link;
        `targets()` the linked peers that take `files`; `peer(id)` its (host, port, token, name).
        `received()` is the Downloads/Beamer folder; `dropped(incoming)` arms the landing for a dragged set,
        which calls `deliver` when the button is let go; `opened(folder)` shows a folder."""
        self._clipboard = clipboard
        self._own_id = own_id
        self._staging = staging
        self._send = send
        self._targets = targets
        self._peer = peer
        self._notify = notify
        self.paste_seconds = paste_seconds
        self._fetch = fetch
        self._max_bytes = max_bytes
        self._received = received
        self._dropped = dropped
        self._opened = opened
        # What this PC may offer: an elevated Beamer offers only what its user could read itself.
        self._may_read = may_read
        # At most this many fetches at once: a peer's offers cannot start them without end.
        self._fetching = threading.BoundedSemaphore(FETCHES_AT_ONCE)
        self._lock = threading.RLock()
        self._offers: Dict[bytes, dict] = {}     # what this PC offered: id -> items, sources, made
        self._latest: Optional[bytes] = None
        self._incoming: Optional[dict] = None    # the newest peer offer not yet fetched
        self._written = set()                    # clipboard sequences this PC wrote itself
        self._seen_sequence = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.busy = False

    # -- life ---------------------------------------------------------------------------------

    def start(self) -> None:
        self.purge()
        if self._thread is None:
            self._seen_sequence = self._clipboard.sequence()
            self._thread = threading.Thread(target=self._watch, name="Beamer-files-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def purge(self, now: Optional[float] = None) -> None:
        """Staging folders left over from before (a quit mid-way, a crash) older than a day."""
        now = time.time() if now is None else now
        try:
            entries = list(os.scandir(self._staging))
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False) and now - entry.stat().st_mtime > PURGE_OLDER_SECONDS:
                    shutil.rmtree(entry.path, ignore_errors=True)
            except OSError:
                pass

    # -- offering what this PC copied ---------------------------------------------------------

    def _watch(self) -> None:
        while not self._stop.wait(WATCH_SECONDS):
            try:
                self.check()
            except Exception:
                LOGGER.exception("Could not check the clipboard for files")

    def check(self) -> None:
        """One look at the clipboard: files copied here are offered, anything else withdraws the
        last offer. Files this PC put there itself are never offered back."""
        current = self._clipboard.sequence()
        if current == self._seen_sequence:
            return
        self._seen_sequence = current
        if current in self._written:
            return
        paths = self._clipboard.read_files()
        with self._lock:
            previous, self._latest = self._latest, None
        if previous is not None:
            self._broadcast(protocol.files_msg("cancel", previous))
        if not paths:
            return
        offer = os.urandom(16)
        try:
            items, sources, total = file_transfer.build_manifest(paths, self._max_bytes, self._may_read)
        except file_transfer.TooLarge:
            items, sources, total = [], [], self._max_bytes + 1
        if not items and total <= self._max_bytes:
            return
        with self._lock:
            now = time.time()
            self._offers = {key: value for key, value in self._offers.items() if now - value["made"] < OFFER_SECONDS}
            self._offers[offer] = {"items": items, "sources": sources, "made": now}
            self._latest = offer
        label = label_for(items) if items else tr("Files over {size}").format(size=_size(self._max_bytes))
        count = max(1, sum(1 for item in items if not item["dir"]) or len(items))
        LOGGER.info("Offering %s (%s, %s) to the PCs that take files", label, count, _size(total))
        self._broadcast(protocol.files_msg("offer", offer, count=count, total=total, label=label))

    def offer_paths(self, paths: List[str], peer: bytes, mode: str) -> Optional[str]:
        """Offers `paths` to `peer` alone, to `drop` where the dragged files are let go there or to
        `send` to its downloads. The set's label, or None when there is nothing to send."""
        try:
            items, sources, total = file_transfer.build_manifest(paths, SEND_MAX_BYTES, self._may_read)
        except file_transfer.TooLarge as exc:
            self._notify(tr("Those files are too large to send: {why}.").format(why=exc))
            return None
        if not items:
            return None
        offer = os.urandom(16)
        with self._lock:
            self._offers[offer] = {"items": items, "sources": sources, "made": time.time()}
        label = label_for(items)
        count = max(1, sum(1 for item in items if not item["dir"]))
        LOGGER.info("Offering %s (%s, %s) to %s to %s", label, count, _size(total), protocol.id_text(peer), mode)
        self._send(peer, protocol.files_msg("offer", offer, count=count, total=total, label=label, mode=mode))
        return label

    def _broadcast(self, message: dict) -> None:
        for peer in self._targets():
            try:
                self._send(peer, message)
            except Exception:
                LOGGER.exception("Could not tell a peer about the copied files")

    def serve(self, request) -> None:
        """A peer's data connection asking for an offer (receiver._DataRequest)."""
        with self._lock:
            held = self._offers.get(request.offer)
            fresh = held is not None and time.time() - held["made"] < OFFER_SECONDS and held["items"]
        if not fresh:
            file_transfer.refuse(request.sock, request.session, tr("Those files are no longer offered: copy them again."))
            return
        LOGGER.info("Sending %s to %s", label_for(held["items"]), request.entry.get("name"))
        file_transfer.serve(request.sock, request.session, held["items"], held["sources"], self._stop)

    # -- taking what a peer copied ------------------------------------------------------------

    def on_message(self, peer: bytes, read) -> None:
        action, offer, count, total, label, mode = read
        incoming = {"peer": peer, "offer": offer, "count": count, "total": total, "label": label,
                    "mode": mode, "at": time.time()}
        if action == "offer" and mode == "send":
            self._spawn(self.deliver, incoming, self._received(), True)
            return
        if action == "offer" and mode == "drop":
            if self._dropped is not None:
                self._dropped(incoming)
            else:
                self._spawn(self.deliver, incoming, self._received(), True)
            return
        with self._lock:
            if action == "cancel":
                if self._incoming is not None and self._incoming["offer"] == offer:
                    self._incoming = None
                return
            self._incoming = incoming

    def activate(self) -> None:
        """Input arrived on this PC: fetch the newest offer, if it is fresh, and stage it to paste."""
        with self._lock:
            incoming, self._incoming = self._incoming, None
        if incoming is None or time.time() - incoming["at"] > OFFER_SECONDS:
            return
        if incoming["total"] > self._max_bytes:
            self._notify(tr("{label} are too large to paste here (over {size}).").format(
                label=incoming["label"], size=_size(self._max_bytes)))
            return
        self._spawn(self._receive_to_clipboard, incoming)

    def _receive_to_clipboard(self, incoming: dict) -> None:
        tops = self._receive(incoming, max_bytes=min(self._max_bytes, incoming["total"]))
        if tops is None:
            return
        destination = os.path.dirname(tops[0])
        written = self._clipboard.write_files(tops)
        if written is None:
            self._notify(tr("{label} arrived but could not be put on the clipboard.").format(label=incoming["label"]))
            return
        with self._lock:
            self._written.add(written)
        minutes = max(1, round(self.paste_seconds / 60))
        self._notify(tr("{label} from {name}: paste within {minutes} min.").format(
            label=incoming["label"], name=self._name(incoming), minutes=minutes))
        self._later(self.paste_seconds, lambda: self._expire(written, destination))

    def _name(self, incoming: dict) -> str:
        found = self._peer(incoming["peer"])
        return found[3] if found else ""

    def _receive(self, incoming: dict, max_bytes: int) -> Optional[List[str]]:
        """Fetches a set into its own staging folder, each file marked from another computer. The
        top-level paths written, or None once the failure has been told."""
        found = self._peer(incoming["peer"])
        if found is None:
            return None
        host, port, token, name = found
        destination = os.path.join(self._staging, protocol.id_text(incoming["offer"]))
        if not self._fetching.acquire(blocking=False):
            LOGGER.warning("Not receiving %s from %s: %d transfers are already running", incoming["label"], name,
                           FETCHES_AT_ONCE)
            return None
        self.busy = True
        try:
            os.makedirs(destination, exist_ok=False)
            if incoming["total"] >= LARGE_BYTES:
                self._notify(tr("Receiving {label} ({size}) from {name}…").format(
                    label=incoming["label"], size=_size(incoming["total"]), name=name))
            started = time.monotonic()
            tops = self._fetch(host, port, token, self._own_id, incoming["offer"], destination,
                               cancelled=self._stop, written=self._clipboard.mark_from_another_computer,
                               max_bytes=max_bytes)
            seconds = max(time.monotonic() - started, 0.001)
            LOGGER.info("Received %s from %s: %s in %.1f s (%s/s)", incoming["label"], name,
                        _size(incoming["total"]), seconds, _size(int(incoming["total"] / seconds)))
            return tops
        except FileExistsError:
            return None
        except (file_transfer.TransferError, OSError, protocol.ProtocolError) as exc:
            LOGGER.warning("Receiving files from %s failed: %s", name, exc)
            self._notify(tr("Could not receive {label} from {name}: {why}").format(
                label=incoming["label"], name=name, why=exc))
            shutil.rmtree(destination, ignore_errors=True)
            return None
        finally:
            self.busy = False
            self._fetching.release()

    def deliver(self, incoming: dict, folder: str, show: bool = False) -> Optional[List[str]]:
        """Fetches a dropped or sent set into `folder`, each top item under a free name there, and
        says where it went. The paths written, or None."""
        found = self._peer(incoming["peer"])
        if found is None or not folder:
            return None
        name = found[3]
        try:
            os.makedirs(folder, exist_ok=True)
            free = shutil.disk_usage(folder).free
        except OSError as exc:
            self._notify(tr("Could not receive {label}: {why}").format(label=incoming["label"], why=exc))
            return None
        if incoming["total"] > min(SEND_MAX_BYTES, free - 64 * 1024 ** 2):
            self._notify(tr("Not enough room in {folder} for {label} ({size}).").format(
                folder=folder, label=incoming["label"], size=_size(incoming["total"])))
            return None
        # Held to the size the offer claimed, which the free-space check above used.
        tops = self._receive(incoming, max_bytes=min(SEND_MAX_BYTES, incoming["total"]))
        if tops is None:
            return None
        placed = []
        for top in tops:
            target = _free_name(folder, os.path.basename(top))
            try:
                shutil.move(top, target)
                placed.append(target)
            except OSError as exc:
                LOGGER.warning("Could not move %s into %s: %s", top, folder, exc)
        if tops:
            shutil.rmtree(os.path.dirname(tops[0]), ignore_errors=True)
        self._notify(tr("{label} from {name} copied to {folder}.").format(label=incoming["label"], name=name, folder=folder))
        if show and self._opened is not None:
            self._opened(folder)
        return placed

    def _expire(self, written: int, destination: str) -> None:
        if self._clipboard.clear_if(written):
            LOGGER.info("The received files were not pasted in time and left the clipboard")
        self._later(DELETE_AFTER_SECONDS, lambda: shutil.rmtree(destination, ignore_errors=True))

    @staticmethod
    def _spawn(target, *args) -> None:
        threading.Thread(target=target, args=args, name="Beamer-files-fetch", daemon=True).start()

    @staticmethod
    def _later(seconds: float, action) -> None:
        timer = threading.Timer(seconds, action)
        timer.daemon = True
        timer.start()


def _free_name(folder: str, name: str) -> str:
    """`name` in `folder`, or "name (2).ext" and on until one is free."""
    stem, dot, ext = name.rpartition(".") if "." in name[1:] else (name, "", "")
    candidate, number = os.path.join(folder, name), 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{stem} ({number}){dot}{ext}")
        number += 1
    return candidate
