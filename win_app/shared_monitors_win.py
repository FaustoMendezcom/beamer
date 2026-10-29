"""Shared monitors on Windows: which of this PC's monitors are also wired to a peer, which machine
each shows, and switching them over DDC/CI (core.shared_monitors for the rules).

A worker thread reads each identified monitor's input every POLL_SECONDS: a value is this PC's own
input code on it (learned, never configured), and no answer means it shows another machine. It
also repeats the pointer fence while monitors are away, as Windows drops a clip whenever another
window takes the foreground. The app hears of every change through `changed()`, on this thread,
and reads `snapshot()`; it sends the messages and moves the picture itself."""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 2.0
FENCE_SECONDS = 0.25
# A monitor takes a moment to change input and stop answering; read again after this.
SETTLE_SECONDS = 1.5


class SharedMonitors:
    def __init__(self, ddc, desktop, *, codes: Dict[str, int], peers: Dict[str, List[dict]],
                 changed: Callable[[], None], learned: Callable[[Dict[str, int]], None]):
        """`ddc` is monitors_ddc (or a fake), `desktop` desktop_win; `codes` and `peers` as
        settings keep them (shared_monitors.state); `learned(codes)` saves a newly read own code."""
        self._ddc = ddc
        self._desktop = desktop
        self._codes = dict(codes)
        self._peers = {peer: list(listed) for peer, listed in peers.items()}
        self._changed = changed
        self._learned = learned
        self._lock = threading.RLock()
        self._monitors = []                  # monitors_ddc.Monitor, identified ones only
        self._display_count = 0              # every display, identified or not
        self._showing: Dict[str, bool] = {}  # monitor id: whether it shows this PC
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- life ---------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="Beamer-shared-monitors", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._desktop.set_away(())

    def poll_now(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_poll or self._wake.is_set():
                self._wake.clear()
                try:
                    self.poll()
                except Exception:
                    LOGGER.exception("Could not read the shared monitors")
                next_poll = time.monotonic() + POLL_SECONDS
            if self.away():
                try:
                    self._desktop.fence()
                except Exception:
                    LOGGER.exception("Could not keep the pointer off the away monitors")
            self._wake.wait(FENCE_SECONDS)

    # -- state --------------------------------------------------------------

    def poll(self) -> bool:
        """Reads every identified monitor; True (and `changed()`) when what this PC knows moved."""
        every = self._ddc.monitors()
        found = [monitor for monitor in every if monitor.edid_id]
        readings = {monitor.edid_id: self._ddc.read_input(monitor.device) for monitor in found}
        with self._lock:
            before = (self._away_locked(), dict(self._codes), [m.key for m in self._monitors])
            self._monitors = found
            self._display_count = len(every)
            learned = False
            for identity, code in readings.items():
                self._showing[identity] = code is not None
                if code is not None and self._codes.get(identity) != code:
                    self._codes[identity] = code
                    learned = True
            after = (self._away_locked(), dict(self._codes), [m.key for m in self._monitors])
            codes = dict(self._codes)
        if learned:
            self._learned(codes)
        if before != after:
            self._apply_fence()
            self._changed()
            return True
        return False

    def set_peer(self, peer: str, monitors: List[dict]) -> None:
        """A peer's `announce`: the monitors it has, its codes on them and what each shows."""
        with self._lock:
            before = self._away_locked()
            self._peers[peer] = list(monitors)
            after = self._away_locked()
        if before != after:
            self._apply_fence()
        self._changed()

    def forget_peer(self, peer: str) -> None:
        with self._lock:
            self._peers.pop(peer, None)
        self._apply_fence()
        self._changed()

    def shared(self) -> Dict[str, List[dict]]:
        """{peer id: [{id, code, display, showing}]} as that peer announced them, for the
        monitors this PC also has (core.shared_monitors.shared_with)."""
        from core import shared_monitors

        with self._lock:
            return shared_monitors.shared_with([m.edid_id for m in self._monitors], self._peers)

    def away(self) -> Dict[str, str]:
        """{display key: peer id} for this PC's shared monitors now showing that peer."""
        with self._lock:
            return self._away_locked()

    def _away_locked(self) -> Dict[str, str]:
        from core import shared_monitors

        own = {m.edid_id: m for m in self._monitors}
        result = {}
        for peer, listed in shared_monitors.shared_with(own, self._peers).items():
            for monitor in listed:
                # Both ends must agree: a monitor asleep or off answers neither machine, and is not away.
                if self._showing.get(monitor["id"]) is False and monitor.get("showing") is True:
                    result[own[monitor["id"]].key] = peer
        if result and len(result) >= self._display_count:
            # Every display away leaves nothing to fence the pointer onto; hide none.
            return {}
        return result

    def moves_picture(self) -> bool:
        """Whether this PC gives its shared monitors' place in the picture to the peer they show:
        only one with a display of its own that no peer shares, so of the two machines wired to
        them exactly one moves tiles (on the other, its only displays are the shared ones)."""
        from core import shared_monitors

        with self._lock:
            shared = {monitor["id"] for listed in shared_monitors.shared_with(
                [m.edid_id for m in self._monitors], self._peers).values() for monitor in listed}
            return self._display_count > len([m for m in self._monitors if m.edid_id in shared])

    def announcement(self) -> List[dict]:
        """This PC's monitors for an `announce`: those with a known own code."""
        with self._lock:
            return [{"id": m.edid_id, "code": self._codes[m.edid_id], "display": m.key,
                     "showing": self._showing.get(m.edid_id, False)}
                    for m in self._monitors if m.edid_id in self._codes]

    def snapshot(self) -> dict:
        with self._lock:
            return {"away": self._away_locked(), "codes": dict(self._codes),
                    "peers": {peer: list(listed) for peer, listed in self._peers.items()}}

    def _apply_fence(self) -> None:
        away = self.away()
        rects = []
        with self._lock:
            for monitor in self._monitors:
                if monitor.key in away:
                    rects.append(monitor.rect)
        self._desktop.set_away(rects)

    # -- switching ----------------------------------------------------------

    def showing_here(self, peer: str) -> List[dict]:
        """The monitors shared with `peer` that show this PC, as `peer` announced them."""
        with self._lock:
            own = {m.edid_id: m for m in self._monitors}
            return [monitor for monitor in self.shared().get(peer, [])
                    if monitor["id"] in own and self._showing.get(monitor["id"]) is True]

    def taken(self, peer: str) -> None:
        """`peer`'s `give`: the monitors it shares with this PC show this PC now. Taken at its word
        at once, so input arriving next lands on them, and confirmed by reading after the settle."""
        with self._lock:
            listed = self.shared().get(peer, [])
            ids = {monitor["id"] for monitor in listed}
            for identity in ids:
                self._showing[identity] = True
            self._peers[peer] = [dict(item, showing=False) if item.get("id") in ids else item
                                 for item in self._peers.get(peer, [])]
        if ids:
            self._apply_fence()
            self._changed()
        threading.Timer(SETTLE_SECONDS, self.poll_now).start()

    def give(self, peer: str) -> List[str]:
        """Switches every monitor shared with `peer` that shows this PC to `peer`'s input. The
        display keys switched, empty when none showed this PC."""
        with self._lock:
            own = {m.edid_id: m for m in self._monitors}
            targets = [(own[monitor["id"]], monitor["code"]) for monitor in self.showing_here(peer)]
        switched = []
        for monitor, code in targets:
            if self._ddc.set_input(monitor.device, code):
                switched.append(monitor.key)
                LOGGER.info("Switched %s (%s) to %s's input %#04x", monitor.device, monitor.edid_id, peer, code)
            else:
                LOGGER.warning("%s did not take the switch to %s's input", monitor.device, peer)
        if switched:
            with self._lock:
                gone = {monitor.edid_id for monitor, _code in targets if monitor.key in switched}
                for identity in gone:
                    self._showing[identity] = False
                # Until the peer's next `announce` says so itself: it now shows them.
                self._peers[peer] = [dict(item, showing=True) if item.get("id") in gone else item
                                     for item in self._peers.get(peer, [])]
            self._apply_fence()
            self._changed()
            threading.Timer(SETTLE_SECONDS, self.poll_now).start()
        return switched
