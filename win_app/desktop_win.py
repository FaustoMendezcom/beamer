"""Win32 pointer and monitor geometry, imported lazily by the receiver so the
crossing logic stays testable without ctypes.windll. GetCursorPos, SetCursorPos
and EnumDisplayMonitors share one coordinate space — virtual-desktop pixels,
DPI-virtualised identically for this process — so values from one feed the
others unconverted."""

import atexit
import ctypes
import logging
import os
import sys
import threading
import time
from typing import List, Tuple

from core.return_edge import Rect

_IS_WINDOWS = sys.platform == "win32"
CAN_HIDE_POINTER = _IS_WINDOWS
user32 = ctypes.WinDLL("user32", use_last_error=True) if _IS_WINDOWS else None
LOGGER = logging.getLogger(__name__)

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


if _IS_WINDOWS:
    MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), ctypes.c_ssize_t)
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.ClipCursor.argtypes = [ctypes.c_void_p]
    user32.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.c_void_p, MONITORENUMPROC, ctypes.c_ssize_t]
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.CreateCursor.restype = ctypes.c_void_p
    user32.CreateCursor.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p
    ]
    user32.SetSystemCursor.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    user32.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.OpenInputDesktop.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    user32.GetUserObjectInformationW.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)
    ]
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def _require() -> None:
    if user32 is None:
        raise RuntimeError("Win32 desktop geometry is only available on Windows")


# Shared monitors showing another machine (shared_monitors_win): still attached, so Windows
# keeps them on the desktop, but no longer this machine's to use. Held as their rectangles,
# and left out of `monitors` and `display_map` so edges and zones see the desktop as it shows.
_away: frozenset = frozenset()


def _all_monitors() -> List[Rect]:
    _require()
    found: List[Rect] = []

    def collect(_monitor, _dc, rect, _lparam):
        r = rect.contents
        found.append(Rect(r.left, r.top, r.right - r.left, r.bottom - r.top))
        return 1

    if not user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(collect), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return found


def monitors() -> List[Rect]:
    found = _all_monitors()
    kept = [rect for rect in found if rect not in _away]
    return kept or found


def display_map() -> dict:
    """{key: rect}, keyed by enumeration order across every monitor, so hiding one does not
    renumber the others the zones name."""
    found = _all_monitors()
    kept = {str(index): rect for index, rect in enumerate(found) if rect not in _away}
    return kept or {str(index): rect for index, rect in enumerate(found)}


def wake_displays() -> None:
    """Turns this PC's screens on if Windows had turned them off for inactivity: a pointer nudge
    there and back (input is what Windows wakes a display for) and a request that the display
    stay on, which also resets the idle timer. Nothing moves that anyone would see."""
    if user32 is None:
        return
    import input_injector

    try:
        input_injector.inject_mouse_move(1, 0)
        input_injector.inject_mouse_move(-1, 0)
    except OSError:
        pass
    ES_DISPLAY_REQUIRED, ES_SYSTEM_REQUIRED = 0x00000002, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]


def idle_seconds() -> float:
    """How long since this PC's last input, from anyone: a driving machine's injected input too."""
    if user32 is None:
        return 0.0
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return max(0.0, ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0)


def display_map_all() -> dict:
    """`display_map` with the away monitors too: what a machine that does not move its own tile
    for shared monitors puts in the picture."""
    return {str(index): rect for index, rect in enumerate(_all_monitors())}


def set_away(rects) -> None:
    """Hides these monitor rectangles from `monitors` and `display_map`, and fences the pointer
    off them (`fence`); empty shows every monitor again and lifts the fence."""
    global _away
    _away = frozenset(rects)
    fence()


def fence() -> None:
    """Keeps the pointer on the monitors this machine shows: ClipCursor to their bounds when they
    form one rectangle, released when nothing is away. Windows drops a clip when another window
    takes the foreground, so the caller repeats this while monitors are away."""
    if user32 is None:
        return
    if not _away:
        user32.ClipCursor(None)
        return
    kept = [rect for rect in _all_monitors() if rect not in _away]
    if not kept:
        user32.ClipCursor(None)
        return
    left = min(rect.x for rect in kept)
    top = min(rect.y for rect in kept)
    right = max(rect.x + rect.width for rect in kept)
    bottom = max(rect.y + rect.height for rect in kept)
    if sum(rect.width * rect.height for rect in kept) != (right - left) * (bottom - top):
        # Not one rectangle (an L of displays): a clip to the bounds would take in an away one.
        return
    user32.ClipCursor(ctypes.byref(RECT(left, top, right, bottom)))


def cursor_position() -> Tuple[int, int]:
    _require()
    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return point.x, point.y


_refused_under = [None]


def set_cursor_position(x: int, y: int) -> None:
    set_cursor_position_with_callback(x, y)


def set_cursor_position_with_callback(x: int, y: int, on_placed=None, *, place_if_current=None) -> None:
    """Report blocked placement without claiming that the receiver arrived."""
    _require()
    x, y = int(x), int(y)
    error = None
    def place():
        nonlocal error
        # Capture here: the ownership guard and foreground queries also call Win32.
        ctypes.set_last_error(0)
        if user32.SetCursorPos(x, y):
            _refused_under[0] = None
            if on_placed is not None:
                on_placed()
            return True
        error = ctypes.get_last_error() or None
        return False

    # The warp and its completion share the receiver's ownership lock.
    result = place_if_current(place) if place_if_current is not None else place()
    if result is not False:
        return
    import input_injector

    blocker = input_injector.foreground_blocker()
    input_injector.report_input_problem(blocker, operation="SetCursorPos", error=error)
    # A hold at the return edge lands here on every move, so once per blocker.
    if blocker.key != _refused_under[0]:
        _refused_under[0] = blocker.key
        LOGGER.warning(
            "SetCursorPos refused (last_error=%s): process=%r pid=%s class=%r elevated=%s integrity=%s query_error=%s",
            error, blocker.process_name, blocker.pid, blocker.window_class,
            blocker.elevated, blocker.integrity, blocker.query_error,
        )
    def retry():
        place_if_current(place)

    if blocker.window_class == input_injector.GAMEINPUT_WINDOW_CLASS:
        # Sender pins and return-edge corrections have no visit guard and can become stale.
        input_injector.release_gameinput_foreground(after=retry if place_if_current is not None else None)


# What SHQueryUserNotificationState says while something owns the whole screen. 3 is a Direct3D
# exclusive full-screen game and 4 is Presentation Settings, both unambiguous. 2, QUNS_BUSY, is
# also what a borderless game or a browser on F11 gives, but overlays such as NVIDIA's raise it
# with nothing full screen at all (PowerToys PR #45891 split it out of its own guard for that), so
# it counts only with the foreground window covering its whole monitor. A maximised window stops
# at the taskbar and never does, which is what keeps this from becoming the size test the Mac
# learned catches every zoomed window.
_EXCLUSIVE_STATES = {3, 4}
QUNS_BUSY = 2
# The desktop and the taskbar are monitor-sized and foreground whenever the desktop is clicked.
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def full_screen_app():
    """The name of the foreground app while it is full screen, else None. GUI thread, once a
    second, as the Mac checks."""
    _require()
    pid = _foreground_process_id()
    if pid is None or pid == os.getpid():
        return None
    state = ctypes.c_int(0)
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return None
    if state.value in _EXCLUSIVE_STATES or (state.value == QUNS_BUSY and _foreground_covers_its_monitor()):
        return _foreground_app_name() or "An app"
    return None


def _foreground_process_id():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]


def _foreground_covers_its_monitor() -> bool:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    name = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, name, 64)
    if name.value in _SHELL_CLASSES:
        return False
    window = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(window)):
        return False
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    screen = info.rcMonitor
    return (window.left, window.top, window.right, window.bottom) == (
        screen.left, screen.top, screen.right, screen.bottom
    )


# The hook reports logical buttons; GetAsyncKeyState reads physical ones, so with the buttons
# swapped in Settings the left and right virtual keys trade places.
_BUTTON_VKS = {"left": 0x01, "right": 0x02, "middle": 0x04, "back": 0x05, "forward": 0x06}


def button_down(name: str) -> bool:
    _require()
    vk = _BUTTON_VKS.get(name)
    if vk is None:
        return False
    if vk in (0x01, 0x02) and user32.GetSystemMetrics(23):  # SM_SWAPBUTTON
        vk = 0x03 - vk
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _foreground_app_name():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle =kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = ctypes.c_ulong(260)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        name = buffer.value.rsplit("\\", 1)[-1]
        return name[:-4] if name.lower().endswith(".exe") else name
    finally:
        kernel32.CloseHandle(handle)


# Every system cursor a pointer can show as, the OCR_* ids SetSystemCursor takes.
_SYSTEM_CURSORS = (
    32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644, 32645, 32646, 32648, 32649, 32650, 32651, 32671,
    32672,
)
_SPI_SETCURSORS = 0x0057
_DESKTOP_READOBJECTS = 0x0001
_UOI_NAME = 2
_GUARD_SECONDS = 0.25
_pointer_lock = threading.Lock()
_pointer_hidden_for = set()
_guard = [0]
_exit_hook = [False]


_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x20
_WS_EX_LAYERED = 0x80000


def set_click_through(hwnd: int, through: bool) -> None:
    """Let clicks pass through a window, or stop them. Safe from any thread and immediate, which a
    Qt hide queued for the window's own thread is not."""
    _require()
    style = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    wanted = (style | _WS_EX_TRANSPARENT | _WS_EX_LAYERED) if through else (style & ~_WS_EX_TRANSPARENT)
    if wanted != style:
        user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, wanted)


def _pointer_marker():
    from core import portable_runtime
    if portable_runtime.enabled():
        return str(portable_runtime.data_path() / "pointer-hidden")
    base = os.environ.get("LOCALAPPDATA")
    return os.path.join(base, "Beamer", "pointer-hidden") if base else None


def hide_pointer(reason: str) -> None:
    """Hide the pointer everywhere until show_pointer(reason) and every other reason is shown again.

    ShowCursor only reaches this process's own windows, so the system cursors themselves are swapped
    for a blank one, and given back with SPI_SETCURSORS, which reloads the user's own scheme, size and
    colour included. That outlives the process, so a marker file stands for the hide until it is
    undone: if Beamer dies first, restore_pointer at the next start gives the pointer back, and any
    Python exit gives it back on the way out.

    The lock screen, Ctrl+Alt+Del and a UAC prompt share the session's cursors but not the hooks, so
    a pointer hidden while driving would be a blank one moving over a password box. A guard gives the
    cursors back for as long as another desktop has the input, and blanks them again on return."""
    _require()
    with _pointer_lock:
        first = not _pointer_hidden_for
        _pointer_hidden_for.add(reason)
        if not first:
            return
        marker = _pointer_marker()
        if marker:
            try:
                os.makedirs(os.path.dirname(marker), exist_ok=True)
                open(marker, "w").close()
            except OSError:
                LOGGER.exception("Could not write the pointer-hidden marker")
        if not _exit_hook[0]:
            _exit_hook[0] = True
            atexit.register(restore_pointer)
        _blank_cursors()
        _guard[0] += 1
        threading.Thread(target=_guard_desktop, args=(_guard[0],), name="Beamer-pointer-guard", daemon=True).start()


def _blank_cursors() -> None:
    and_mask = (ctypes.c_ubyte * 128)(*([0xFF] * 128))
    xor_mask = (ctypes.c_ubyte * 128)()
    for cursor_id in _SYSTEM_CURSORS:
        # SetSystemCursor takes the handle over and destroys it, so each id gets its own.
        blank = user32.CreateCursor(None, 0, 0, 32, 32, and_mask, xor_mask)
        if not blank or not user32.SetSystemCursor(blank, cursor_id):
            LOGGER.warning("Could not blank system cursor %s (error %s)", cursor_id, ctypes.get_last_error())


def _on_default_desktop() -> bool:
    """False while the lock screen, the secure attention screen or UAC has the input: Beamer may not
    even open those, which is itself the answer."""
    desktop = user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not desktop:
        return False
    try:
        name = ctypes.create_unicode_buffer(64)
        needed = ctypes.c_ulong(0)
        if not user32.GetUserObjectInformationW(desktop, _UOI_NAME, name, ctypes.sizeof(name), ctypes.byref(needed)):
            return False
        return name.value.lower() == "default"
    finally:
        user32.CloseDesktop(desktop)


def _guard_desktop(generation: int) -> None:
    blanked = True
    while True:
        time.sleep(_GUARD_SECONDS)
        with _pointer_lock:
            if _guard[0] != generation or not _pointer_hidden_for:
                return
            try:
                here = _on_default_desktop()
            except Exception:
                LOGGER.exception("Could not read the input desktop; showing the pointer")
                here = False
            if blanked and not here:
                LOGGER.info("Another desktop has the input; the pointer is shown until it returns")
                user32.SystemParametersInfoW(_SPI_SETCURSORS, 0, None, 0)
                blanked = False
            elif here and not blanked:
                _blank_cursors()
                blanked = True


def show_pointer(reason: str) -> None:
    _require()
    with _pointer_lock:
        if reason not in _pointer_hidden_for:
            return
        _pointer_hidden_for.discard(reason)
        if not _pointer_hidden_for:
            _restore_pointer()


def restore_pointer(always: bool = False) -> None:
    """Give back a pointer a previous run hid and never showed, and every hide on the way out.
    `always` at start: the blank cursors are the session's, so a Beamer that ran as another account
    (elevated with an administrator's credentials) left its marker in that account's folder."""
    _require()
    with _pointer_lock:
        marker = _pointer_marker()
        if always or _pointer_hidden_for or (marker and os.path.exists(marker)):
            _pointer_hidden_for.clear()
            _restore_pointer()


def _restore_pointer() -> None:
    _guard[0] += 1
    if not user32.SystemParametersInfoW(_SPI_SETCURSORS, 0, None, 0):
        LOGGER.warning("Could not reload the system cursors (error %s)", ctypes.get_last_error())
        return
    marker = _pointer_marker()
    if marker:
        try:
            os.remove(marker)
        except FileNotFoundError:
            pass
        except OSError:
            LOGGER.exception("Could not remove the pointer-hidden marker")
