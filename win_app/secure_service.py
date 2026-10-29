"""The BeamerSecureDesktop service (docs/secure-desktop.md): `BeamerSecure.exe --service`, as
LocalSystem, from C:\\Program Files\\Beamer, installed by Install-Maintenance.ps1 -SecureDesktop.

It keeps one secure desktop helper running as SYSTEM in the active console session: started when
a session takes the console, restarted if it ends, and replaced when the console moves to another
session (fast user switching, a remote session ending). It does nothing else."""

import logging
import os
import sys
from typing import Optional

LOGGER = logging.getLogger(__name__)
SERVICE_NAME = "BeamerSecureDesktop"
DISPLAY_NAME = "Beamer secure desktop input"
DESCRIPTION = ("Lets Beamer, running as administrator, type on the secure desktop: UAC prompts, the "
               "lock screen and Ctrl+Alt+Del.")
CHECK_SECONDS = 2.0
NO_SESSION = 0xFFFFFFFF
LOG_FOLDER = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Beamer")


class Supervisor:
    """Which helper should run, apart from the service plumbing so it can be tested: `session()`
    is the console session (NO_SESSION while none has it), `launch(session)` starts a helper and
    returns its process, `alive(process)` and `stop(process)` look after it."""

    def __init__(self, session, launch, alive, stop):
        self._session, self._launch, self._alive, self._stop = session, launch, alive, stop
        self.process = None
        self.running_in: Optional[int] = None

    def check(self) -> None:
        session = self._session()
        if self.process is not None and (session != self.running_in or not self._alive(self.process)):
            LOGGER.info("Secure desktop helper for session %s ends", self.running_in)
            self._stop(self.process)
            self.process, self.running_in = None, None
        if self.process is None and session not in (None, NO_SESSION, 0):
            try:
                self.process = self._launch(session)
                self.running_in = session
                LOGGER.info("Secure desktop helper started in session %s", session)
            except Exception:
                LOGGER.exception("Could not start the secure desktop helper in session %s", session)

    def shutdown(self) -> None:
        if self.process is not None:
            self._stop(self.process)
            self.process, self.running_in = None, None


# -- Win32 ---------------------------------------------------------------------------------------

def console_session() -> int:
    import ctypes

    return int(ctypes.windll.kernel32.WTSGetActiveConsoleSessionId())


def launch_helper(session: int):
    """`<this exe> --secure-helper` as SYSTEM in `session`: this service's own token, its session
    changed, on the user's window station so it can reach that session's desktops."""
    import win32con
    import win32process
    import win32security

    flags = (win32security.TOKEN_DUPLICATE | win32security.TOKEN_ASSIGN_PRIMARY | win32security.TOKEN_QUERY
             | win32security.TOKEN_ADJUST_SESSIONID | win32security.TOKEN_ADJUST_DEFAULT)
    own = win32security.OpenProcessToken(win32process.GetCurrentProcess(), flags)
    token = win32security.DuplicateTokenEx(own, win32security.SecurityImpersonation, flags,
                                           win32security.TokenPrimary, None)
    win32security.SetTokenInformation(token, win32security.TokenSessionId, session)
    startup = win32process.STARTUPINFO()
    startup.lpDesktop = "winsta0\\default"
    exe = sys.executable
    process, thread, _pid, _tid = win32process.CreateProcessAsUser(
        token, exe, f'"{exe}" --secure-helper', None, None, False,
        win32con.CREATE_NO_WINDOW | win32process.CREATE_UNICODE_ENVIRONMENT, None, None, startup)
    thread.Close()
    return process


def process_alive(process) -> bool:
    import win32con
    import win32event

    return win32event.WaitForSingleObject(process, 0) == win32con.WAIT_TIMEOUT


def stop_process(process) -> None:
    import win32process

    try:
        win32process.TerminateProcess(process, 0)
    except Exception:
        pass


def configure_logging(name: str) -> None:
    from logging.handlers import RotatingFileHandler

    try:
        os.makedirs(LOG_FOLDER, exist_ok=True)
        handler = RotatingFileHandler(os.path.join(LOG_FOLDER, name), maxBytes=1024 * 1024, backupCount=2,
                                      encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def run_service() -> None:
    """Hands this process to the service control manager (`--service`)."""
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class Service(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, True, False, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            supervisor = Supervisor(console_session, launch_helper, process_alive, stop_process)
            LOGGER.info("%s running", SERVICE_NAME)
            while win32event.WaitForSingleObject(self.stop_event, int(CHECK_SECONDS * 1000)) == win32event.WAIT_TIMEOUT:
                supervisor.check()
            supervisor.shutdown()
            LOGGER.info("%s stopped", SERVICE_NAME)

    configure_logging("secure-service.log")
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(Service)
    servicemanager.StartServiceCtrlDispatcher()
