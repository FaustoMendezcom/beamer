"""The secure desktop service from Beamer's own settings: what Install-SecureDesktop.ps1 installed on
this PC, and running that script, bundled in Beamer.exe, with Windows' administrator prompt.

The script does the work, exactly as when it is run by hand (docs/secure-desktop.md): this module
only reads the state it leaves and starts it with -ElevatedWindows or -Remove."""

import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger(__name__)
SERVICE_NAME = "BeamerSecureDesktop"
KEY_PATH = r"SOFTWARE\Beamer\SecureDesktop"
SCRIPT_NAME = "Install-SecureDesktop.ps1"
CANCELLED = 1223


@dataclass(frozen=True)
class State:
    installed: bool = False          # the service exists
    for_this_beamer: bool = False    # and lets this Beamer.exe use it
    elevated_windows: bool = False   # with -ElevatedWindows
    outdated: bool = False           # its copy of Beamer.exe is not this one


def script_path() -> Optional[Path]:
    """The installer bundled in Beamer.exe, else the one beside this source file."""
    for base in (getattr(sys, "_MEIPASS", None), Path(__file__).resolve().parent):
        if base is not None and (Path(base) / SCRIPT_NAME).is_file():
            return Path(base) / SCRIPT_NAME
    return None


def _service_exists() -> bool:
    import win32service
    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        service = win32service.OpenService(manager, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
    except Exception:
        return False
    else:
        win32service.CloseServiceHandle(service)
        return True
    finally:
        win32service.CloseServiceHandle(manager)


def _values() -> dict:
    import winreg
    values = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, KEY_PATH) as key:
            for name in ("AllowedClient", "ServiceExe", "ElevatedWindows"):
                try:
                    values[name] = str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    pass
    except OSError:
        pass
    return values


def _same_file(a: str, b: str) -> bool:
    try:
        first, second = os.stat(a), os.stat(b)
    except OSError:
        return False
    # Copy-Item keeps the write time, so an unchanged copy matches in both.
    return first.st_size == second.st_size and int(first.st_mtime) == int(second.st_mtime)


def state(exe: Optional[str] = None) -> State:
    if sys.platform != "win32":
        return State()
    exe = exe or sys.executable
    if not _service_exists():
        return State()
    values = _values()
    allowed = values.get("AllowedClient", "")
    return State(
        installed=True,
        for_this_beamer=bool(allowed) and os.path.normcase(allowed) == os.path.normcase(exe),
        elevated_windows=values.get("ElevatedWindows") == "1",
        outdated=not _same_file(values.get("ServiceExe", ""), exe),
    )


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def command(install: bool, exe: str, script: Path, log: Path) -> str:
    """The PowerShell the elevated prompt runs: the installer, its output kept in `log` so a
    failure can be told, and an exit code that says whether it threw."""
    switches = f"-ClientPath {_quoted(exe)} -ElevatedWindows" if install else "-Remove"
    return (f"try {{ & {_quoted(str(script))} {switches} *>&1 | Out-File -Encoding utf8 {_quoted(str(log))}; exit 0 }} "
            f"catch {{ $_ | Out-File -Append -Encoding utf8 {_quoted(str(log))}; exit 1 }}")


def change(install: bool, exe: Optional[str] = None) -> None:
    """Install (with -ElevatedWindows) or remove the service, after Windows' administrator prompt,
    which a standard account answers with an administrator's password. Raises OSError with a
    sentence for the user when that is cancelled or the script fails."""
    from core.i18n import tr
    import pythoncom
    import win32api
    import win32event
    import win32process
    from win32com.shell import shell

    exe = exe or sys.executable
    script = script_path()
    if script is None:
        raise OSError(tr("This Beamer has no secure desktop installer."))
    descriptor, log_name = tempfile.mkstemp(prefix="beamer-secure-setup-", suffix=".log")
    os.close(descriptor)
    log = Path(log_name)
    arguments = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command(install, exe, script, log)]
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        try:
            result = shell.ShellExecuteEx(
                fMask=0x40 | 0x100, lpVerb="runas", lpFile="powershell.exe",
                lpParameters=subprocess.list2cmdline(arguments), nShow=0)
        except Exception as exc:
            if getattr(exc, "winerror", None) == CANCELLED or (getattr(exc, "args", None) or [None])[0] == CANCELLED:
                raise OSError(tr("Administrator permission was cancelled. Nothing was changed.")) from exc
            raise OSError(tr("Windows could not start the secure desktop installer.")) from exc
        process = result.get("hProcess")
        if process is None:
            raise OSError(tr("Windows could not start the secure desktop installer."))
        try:
            win32event.WaitForSingleObject(process, win32event.INFINITE)
            code = win32process.GetExitCodeProcess(process)
        finally:
            win32api.CloseHandle(process)
    finally:
        pythoncom.CoUninitialize()
    try:
        output = log.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:
        output = ""
    finally:
        try:
            log.unlink()
        except OSError:
            pass
    if output:
        LOGGER.info("The secure desktop installer said:\n%s", output)
    if code != 0:
        last = output.splitlines()[-1] if output else ""
        raise OSError(tr("The secure desktop installer failed: {reason}").format(reason=last or code))
