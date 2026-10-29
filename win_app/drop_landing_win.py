"""Files dragged across an edge (WIRE.md section 10, files): the receiving side.

When a peer offers a dragged set, a small label follows the pointer here ("Release to copy …
here") until the button is let go: by the owner driving this PC (the receiver's button_watch) or
on this PC's own mouse. The set then goes into the folder under the pointer, an Explorer window or
the desktop, and anywhere else into Downloads\\Beamer. No drag is started on this side, so nothing
depends on Windows letting one cross integrity levels, and an elevated Beamer receives too."""

import ctypes
import logging
import sys
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtWidgets import QLabel

from core.i18n import tr

LOGGER = logging.getLogger(__name__)
TICK_MS = 30
WAIT_SECONDS = 20.0
OFFSET = (18, 22)
VK_LBUTTON = 0x01
HWND_TOPMOST = -1
SWP_NOACTIVATE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0010, 0x0001, 0x0040

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint]
else:
    user32 = None


class DropLanding(QObject):
    def __init__(self, cursor: Callable[[], tuple], folder_at: Callable[[int, int], Optional[str]],
                 fallback: Callable[[], str], land: Callable[[dict, str], None], parent=None):
        """`land(incoming, folder)` fetches the set there, off this thread."""
        super().__init__(parent)
        self._cursor, self._folder_at, self._fallback, self._land = cursor, folder_at, fallback, land
        self._incoming: Optional[dict] = None
        self._until = 0.0
        self._was_down = False
        self._label = QLabel()
        self._label.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                                   | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput)
        self._label.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._label.setStyleSheet("QLabel { background: #202124; color: white; padding: 6px 10px;"
                                  " border-radius: 6px; font-size: 12px; }")
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def armed(self) -> bool:
        return self._incoming is not None

    def arm(self, incoming: dict) -> None:
        """A peer's dragged set: follow the pointer until the button is let go."""
        self._incoming = incoming
        self._until = time.monotonic() + WAIT_SECONDS
        self._was_down = bool(user32 and user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        self._label.setText(tr("Release to copy {label} here").format(label=incoming["label"]))
        self._label.adjustSize()
        self._label.show()
        self._timer.start()
        self._tick()

    def released(self, timed_out: bool = False) -> None:
        """The button was let go: by the driving machine, through the receiver, or here. Never let
        go in time, the set goes to the received folder, wherever the pointer is."""
        incoming, self._incoming = self._incoming, None
        self._timer.stop()
        self._label.hide()
        if incoming is None:
            return
        x, y = self._cursor()
        folder = None
        try:
            folder = None if timed_out else self._folder_at(x, y)
        except Exception:
            LOGGER.exception("Could not tell which folder the files were let go over")
        LOGGER.info("Dragged files let go at (%s, %s) over %s", x, y, folder or "no folder")
        self._land(incoming, folder or self._fallback())

    def _tick(self) -> None:
        if self._incoming is None:
            return
        if time.monotonic() > self._until:
            LOGGER.info("Dragged files were not let go in time; they go to the received folder")
            self.released(timed_out=True)
            return
        if user32 is not None:
            down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
            if self._was_down and not down:
                self.released()
                return
            self._was_down = down
        try:
            x, y = self._cursor()
        except Exception:
            return
        if user32 is not None:
            user32.SetWindowPos(int(self._label.winId()), HWND_TOPMOST, x + OFFSET[0], y + OFFSET[1], 0, 0,
                                SWP_NOACTIVATE | SWP_NOSIZE | SWP_SHOWWINDOW)
