"""Physical monitors over DDC/CI (dxva2): each one's EDID identity, and its input source (VCP 0x60)
read and set. Used by shared_monitors_win for monitors wired to two machines.

A monitor answers DDC/CI only from the input it shows: from any other, a read fails or reads 0,
which is how this machine tells a shared monitor is showing the other machine. The value read
while it shows this machine is this machine's own input code on it, whatever the vendor's
numbering (Gigabyte's differ from the MCCS table), so it is learned rather than configured.

Every call opens the physical monitor handles afresh and closes them: they do not survive a
display change, and these calls are rare and run off the input threads."""

import ctypes
import logging
import re
import sys
from dataclasses import dataclass
from typing import List, Optional

from core.return_edge import Rect

LOGGER = logging.getLogger(__name__)
_IS_WINDOWS = sys.platform == "win32"
VCP_INPUT_SOURCE = 0x60
EDD_GET_DEVICE_INTERFACE_NAME = 0x1


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong),
                ("szDevice", ctypes.c_wchar * 32)]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("DeviceName", ctypes.c_wchar * 32), ("DeviceString", ctypes.c_wchar * 128),
                ("StateFlags", ctypes.c_ulong), ("DeviceID", ctypes.c_wchar * 128), ("DeviceKey", ctypes.c_wchar * 128)]


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", ctypes.c_void_p), ("szPhysicalMonitorDescription", ctypes.c_wchar * 128)]


if _IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
    MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT),
                                         ctypes.c_ssize_t)
    user32.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.c_void_p, MONITORENUMPROC, ctypes.c_ssize_t]
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.EnumDisplayDevicesW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(DISPLAY_DEVICEW),
                                           ctypes.c_ulong]
    dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                      ctypes.POINTER(PHYSICAL_MONITOR)]
    dxva2.DestroyPhysicalMonitors.argtypes = [ctypes.c_ulong, ctypes.POINTER(PHYSICAL_MONITOR)]
    dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p,
                                                      ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong)]
    dxva2.SetVCPFeature.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ulong]
else:
    user32 = dxva2 = None


@dataclass(frozen=True)
class Monitor:
    """One display as Windows enumerates it: `key` is desktop_win.display_map's key for it."""
    key: str
    device: str
    rect: Rect
    edid_id: Optional[str]


def edid_identity(edid: bytes) -> Optional[str]:
    """`MFRPROD:SERIAL` from an EDID block: the three-letter maker, product code in hex, and the
    serial-number text descriptor, else the numeric serial. None without a serial, as two
    monitors of one model could not then be told apart."""
    if not isinstance(edid, (bytes, bytearray)) or len(edid) < 128 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    raw = (edid[8] << 8) | edid[9]
    maker = "".join(chr(((raw >> shift) & 0x1F) + 64) for shift in (10, 5, 0))
    product = edid[10] | (edid[11] << 8)
    number = int.from_bytes(edid[12:16], "little")
    serial = ""
    for start in (54, 72, 90, 108):
        block = edid[start:start + 18]
        if block[:3] == b"\x00\x00\x00" and block[3] == 0xFF:
            serial = block[5:18].split(b"\x0a")[0].decode("ascii", "replace").strip()
    serial = re.sub(r"[^A-Za-z0-9_.-]", "", serial) or (str(number) if number else "")
    if not re.fullmatch(r"[A-Z@-Z]{3}", maker) or not serial or serial == "0":
        return None
    return f"{maker}{product:04X}:{serial}"[:64]


def _edid_for(device: str) -> Optional[bytes]:
    """The EDID Windows keeps in the registry for the monitor on adapter output `device`."""
    import winreg

    found = DISPLAY_DEVICEW()
    found.cb = ctypes.sizeof(found)
    if not user32.EnumDisplayDevicesW(device, 0, ctypes.byref(found), EDD_GET_DEVICE_INTERFACE_NAME):
        return None
    # \\?\DISPLAY#GBT3403#5&330c2586&0&UID4354#{e6f07b5f-...}
    parts = found.DeviceID.split("#")
    if len(parts) < 3:
        return None
    path = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{parts[1]}\{parts[2]}\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, _kind = winreg.QueryValueEx(key, "EDID")
    except OSError:
        return None
    return bytes(value)


def _handles() -> list:
    """[(index, device, rect, hmonitor)] in EnumDisplayMonitors' order, desktop_win's keys."""
    found = []

    def collect(monitor, _dc, rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        device = info.szDevice if user32.GetMonitorInfoW(monitor, ctypes.byref(info)) else ""
        r = rect.contents
        found.append((len(found), device, Rect(r.left, r.top, r.right - r.left, r.bottom - r.top), monitor))
        return 1

    if not user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(collect), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return found


def monitors() -> List[Monitor]:
    if user32 is None:
        return []
    result = []
    for index, device, rect, _handle in _handles():
        try:
            identity = edid_identity(_edid_for(device)) if device else None
        except Exception:
            LOGGER.exception("Could not read the EDID of %s", device)
            identity = None
        result.append(Monitor(str(index), device, rect, identity))
    return result


def _with_physical(device: str, action):
    """`action(handle)` on the first physical monitor behind `device`, or None when there is none."""
    for _index, name, _rect, handle in _handles():
        if name != device:
            continue
        count = ctypes.c_ulong()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(handle, ctypes.byref(count)) or not count.value:
            return None
        physical = (PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(handle, count.value, physical):
            return None
        try:
            return action(physical[0].hPhysicalMonitor)
        finally:
            dxva2.DestroyPhysicalMonitors(count.value, physical)
    return None


def read_input(device: str) -> Optional[int]:
    """The input code the monitor on `device` reports, or None when it does not answer, which is
    what a monitor showing another machine's input does (it reads 0)."""
    if dxva2 is None:
        return None

    def read(handle):
        current, maximum = ctypes.c_ulong(), ctypes.c_ulong()
        if not dxva2.GetVCPFeatureAndVCPFeatureReply(handle, VCP_INPUT_SOURCE, None,
                                                     ctypes.byref(current), ctypes.byref(maximum)):
            return None
        value = current.value & 0xFF
        return value or None

    try:
        return _with_physical(device, read)
    except Exception:
        LOGGER.exception("Could not read the input of %s", device)
        return None


def set_input(device: str, code: int) -> bool:
    """Asks the monitor on `device` to show input `code`. True when the command was sent; the
    monitor may still ignore it, so callers confirm by reading."""
    if dxva2 is None or not 1 <= int(code) <= 255:
        return False
    try:
        return bool(_with_physical(device, lambda handle: dxva2.SetVCPFeature(handle, VCP_INPUT_SOURCE, int(code))))
    except Exception:
        LOGGER.exception("Could not switch the input of %s", device)
        return False
