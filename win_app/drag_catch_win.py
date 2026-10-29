"""Files dragged across an edge (WIRE.md section 10, files): the sending side.

While a mouse button is held near an edge whose zone leads to a PC that takes files, an all but
invisible drop target lies along that edge's few outermost pixels. A drag of files pushed through
the edge ends on it: the crossing lets the button go here (the responder releases what the owner
held, or the owner's own button comes up), Explorer drops onto the catcher, and the files are
offered to the zone's machine to land where they are let go there. Nothing else is caught: the
catcher exists only while a button is down by an edge and takes only files.

An elevated Beamer cannot take drops from Explorer (Windows keeps drag and drop from crossing
integrity levels), so the app does not start this when it runs as administrator."""

import ctypes
import logging
import sys
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtWidgets import QWidget

LOGGER = logging.getLogger(__name__)
TICK_MS = 50
NEAR_PIXELS = 120
THICKNESS = 3
VK_LBUTTON = 0x01
HWND_TOPMOST = -1
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint]
else:
    user32 = None


def edge_strip(rect, side: str, span) -> Optional[tuple]:
    """(x, y, w, h) of the drop target along `side` of display `rect` over `span` of it, in
    desktop pixels, or None for a side that is not one."""
    start, end = (span or (0.0, 1.0))
    if side in ("left", "right"):
        top, bottom = rect.y + int(rect.height * start), rect.y + int(rect.height * end)
        x = rect.x if side == "left" else rect.x + rect.width - THICKNESS
        return x, top, THICKNESS, max(1, bottom - top)
    if side in ("top", "bottom"):
        left, right = rect.x + int(rect.width * start), rect.x + int(rect.width * end)
        y = rect.y if side == "top" else rect.y + rect.height - THICKNESS
        return left, y, max(1, right - left), THICKNESS
    return None


def near(strip: tuple, point: tuple, distance: int = NEAR_PIXELS) -> bool:
    x, y, w, h = strip
    px, py = point
    return x - distance <= px <= x + w + distance and y - distance <= py <= y + h + distance


class _Strip(QWidget):
    def __init__(self, peer: str, on_drop: Callable[[str, List[str]], None]):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.peer = peer
        self._on_drop = on_drop
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        # Not fully transparent: a window Windows sees as empty takes no drops.
        self.setWindowOpacity(0.02)

    def place(self, strip: tuple) -> None:
        if not self.isVisible():
            self.show()
        x, y, w, h = strip
        # Desktop pixels, past Qt's own scaling, as the zones and the cursor are.
        user32.SetWindowPos(int(self.winId()), HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    @staticmethod
    def _files(event) -> List[str]:
        data = event.mimeData()
        if not data.hasUrls():
            return []
        paths = [url.toLocalFile() for url in data.urls()]
        return paths if paths and all(paths) else []

    def dragEnterEvent(self, event) -> None:
        if self._files(event):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        paths = self._files(event)
        if not paths:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        LOGGER.info("Files dragged across the edge towards %s: %d item(s)", self.peer, len(paths))
        self._on_drop(self.peer, paths)


class DragCatcher(QObject):
    def __init__(self, zones: Callable[[], list], displays: Callable[[], dict], cursor: Callable[[], tuple],
                 targets: Callable[[], set], on_drop: Callable[[str, List[str]], None], parent=None):
        """`zones()` this PC's zones as settings keep them, `displays()` its display map,
        `targets()` the ids (text) of the peers that take files, `on_drop(peer, paths)` the catch."""
        super().__init__(parent)
        self._zones, self._displays, self._cursor, self._targets = zones, displays, cursor, targets
        self._on_drop = on_drop
        self._strips: Dict[tuple, _Strip] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._released_ticks = 0

    def start(self) -> None:
        if user32 is not None:
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._hide()

    def _hide(self) -> None:
        for strip in self._strips.values():
            strip.hide()

    def _wanted(self, point) -> Dict[tuple, tuple]:
        targets, displays = self._targets(), self._displays()
        wanted = {}
        for zone in self._zones():
            if zone.get("kind") not in ("edge", "part") or zone.get("peer") not in targets:
                continue
            rect = displays.get(zone.get("display"))
            strip = edge_strip(rect, zone.get("side"), zone.get("contact_span")) if rect is not None else None
            if strip is not None and near(strip, point):
                wanted[(zone["peer"], zone.get("display"), zone.get("side"))] = strip
        return wanted

    def _tick(self) -> None:
        if not user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
            # Kept a moment after the button comes up: the drop arrives just after it.
            self._released_ticks += 1
            if self._released_ticks == 6:
                self._hide()
            return
        self._released_ticks = 0
        try:
            wanted = self._wanted(self._cursor())
        except Exception:
            LOGGER.exception("Could not place the drop target for files")
            return
        for key, strip in self._strips.items():
            if key not in wanted:
                strip.hide()
        for key, rect in wanted.items():
            if key not in self._strips:
                self._strips[key] = _Strip(key[0], self._on_drop)
            self._strips[key].place(rect)
