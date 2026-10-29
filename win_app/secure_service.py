"""The BeamerSecureDesktop service (docs/secure-desktop.md): `BeamerSecure.exe --service`, as
LocalSystem, from C:\\Program Files\\Beamer, installed by Install-SecureDesktop.ps1.

It keeps one secure desktop helper running as SYSTEM in the active console session: started when
a session takes the console, restarted if it ends, and replaced when the console moves to another
session (fast user switching, a remote session ending). While nobody is signed in to that session
(the sign-in screen after a start or a sign-out), and the installer copied this PC's pairings, it
also runs Beamer's sign-in screen receiver there (logon_receiver), ended as soon as someone signs
in and their own Beamer takes over."""

import logging
import os
import sys
from typing import Optional

LOGGER = logging.getLogger(__name__)
SERVICE_NAME = "BeamerSecureDesktop"
DISPLAY_NAME = "Beamer secure desktop input"
DESCRIPTION = ("Lets Beamer type on the secure desktop: UAC prompts, the lock screen, the sign-in "
               "screen and Ctrl+Alt+Del.")
CHECK_SECONDS = 2.0
NO_SESSION = 0xFFFFFFFF
# winnt.h; pywin32 does not name it. Needed to move the duplicated token into the user's session.
TOKEN_ADJUST_SESSIONID = 0x0100
# Windows lets only a service send Ctrl+Alt+Del (SendSAS), so the helper, which is not one, sets
# this event and the service sends it. SYSTEM alone may open it.
SAS_EVENT = r"Global\BeamerSecureDesktopSAS"
SAS_EVENT_SDDL = "D:P(A;;GA;;;SY)"
# Logs sit beside the service exe under Program Files, which only administrators can change: a
# folder anyone can make (C:\ProgramData\Beamer) could be swapped for a link and turn SYSTEM's
# log writes and rotations into writes and deletes anywhere.
LOG_FOLDER_NAME = "logs"
ERROR_ALREADY_EXISTS = 183
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class Supervisor:
    """Which helper should run, apart from the service plumbing so it can be tested: `session()`
    is the console session (NO_SESSION while none has it), `launch(session)` starts a helper and
    returns its process, `alive(process)` and `stop(process)` look after it. `wanted(session)`
    says whether it should run in that session at all (the sign-in screen receiver: only while
    nobody is signed in there)."""

    def __init__(self, session, launch, alive, stop, wanted=None, name="Secure desktop helper"):
        self._session, self._launch, self._alive, self._stop = session, launch, alive, stop
        self._wanted = wanted or (lambda session: True)
        self._name = name
        self.process = None
        self.running_in: Optional[int] = None

    def check(self) -> None:
        session = self._session()
        wanted = session not in (None, NO_SESSION, 0) and self._wanted(session)
        if self.process is not None and (session != self.running_in or not wanted or not self._alive(self.process)):
            LOGGER.info("%s for session %s ends", self._name, self.running_in)
            self._stop(self.process)
            self.process, self.running_in = None, None
        if self.process is None and wanted:
            try:
                self.process = self._launch(session)
                self.running_in = session
                LOGGER.info("%s started in session %s", self._name, session)
            except Exception:
                LOGGER.exception("Could not start the %s in session %s", self._name.lower(), session)

    def shutdown(self) -> None:
        if self.process is not None:
            self._stop(self.process)
            self.process, self.running_in = None, None


# -- Win32 ---------------------------------------------------------------------------------------

def console_session() -> int:
    import ctypes

    return int(ctypes.windll.kernel32.WTSGetActiveConsoleSessionId())


def signed_in(session: int) -> bool:
    """Whether someone is signed in to `session`. An error counts as yes, so the sign-in screen
    receiver never holds the port from a user's own Beamer on a guess."""
    import win32ts

    try:
        return bool(win32ts.WTSQuerySessionInformation(None, session, win32ts.WTSUserName))
    except Exception:
        LOGGER.exception("Could not tell whether anyone is signed in to session %s", session)
        return True


def launch_helper(session: int):
    return launch_in(session, "--secure-helper", "winsta0\\default")


def launch_logon_receiver(session: int):
    # On the Winlogon desktop, which has the input at the sign-in screen, so its input and pointer
    # reads go straight there.
    return launch_in(session, "--logon-receiver", "winsta0\\winlogon")


def launch_in(session: int, argument: str, desktop: str):
    """`<this exe> <argument>` as SYSTEM in `session`: this service's own token, its session
    changed, on the user's window station so it can reach that session's desktops."""
    import win32con
    import win32process
    import win32security

    flags = (win32security.TOKEN_DUPLICATE | win32security.TOKEN_ASSIGN_PRIMARY | win32security.TOKEN_QUERY
             | TOKEN_ADJUST_SESSIONID | win32security.TOKEN_ADJUST_DEFAULT)
    own = win32security.OpenProcessToken(win32process.GetCurrentProcess(), flags)
    token = win32security.DuplicateTokenEx(own, win32security.SecurityImpersonation, flags,
                                           win32security.TokenPrimary, None)
    win32security.SetTokenInformation(token, win32security.TokenSessionId, session)
    startup = win32process.STARTUPINFO()
    startup.lpDesktop = desktop
    exe = sys.executable
    # Without the service's own PyInstaller variables, so the helper unpacks itself rather than
    # running from the service's extraction folder, and outlives nothing of it.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("_MEI", "_PYI", "PYINSTALLER"))}
    process, thread, _pid, _tid = win32process.CreateProcessAsUser(
        token, exe, f'"{exe}" {argument}', None, None, False,
        win32con.CREATE_NO_WINDOW | win32process.CREATE_UNICODE_ENVIRONMENT | win32process.CREATE_SUSPENDED,
        environment, None, startup)
    try:
        job = _kill_on_close_job()
        import win32job

        win32job.AssignProcessToJobObject(job, process)
    finally:
        win32process.ResumeThread(thread)
        thread.Close()
    return Launched(process, job)


class Launched:
    """A process the service started and the job holding it and everything it starts: the exe is
    PyInstaller's launcher, which runs the program as a child, so ending the launcher alone left
    the program running (a sign-in screen receiver holding the port after sign-in). The job is
    kill-on-close, so the service ending, by a crash included, ends them too."""

    def __init__(self, process, job):
        self.process, self.job = process, job


def _kill_on_close_job():
    import win32job

    job = win32job.CreateJobObject(None, "")
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits["BasicLimitInformation"]["LimitFlags"] |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    return job


def send_sas() -> None:
    import ctypes

    try:
        ctypes.WinDLL("sas").SendSAS(False)
        LOGGER.info("Sent Ctrl+Alt+Del")
    except OSError:
        LOGGER.exception("Could not send Ctrl+Alt+Del")


def sas_event():
    """The event the helper sets for Ctrl+Alt+Del, made by the service for SYSTEM only, or None
    when another program made one of that name first: its event could be set by anyone, or left
    set, so Ctrl+Alt+Del is not offered at all rather than sent on someone else's say."""
    import win32api
    import win32event
    import win32security

    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        SAS_EVENT_SDDL, win32security.SDDL_REVISION_1)
    event = win32event.CreateEvent(attributes, False, False, SAS_EVENT)
    if win32api.GetLastError() == ERROR_ALREADY_EXISTS:
        LOGGER.error("%s already existed, made by another program; Ctrl+Alt+Del stays off", SAS_EVENT)
        event.Close()
        return None
    return event


def process_alive(launched) -> bool:
    import win32con
    import win32event

    return win32event.WaitForSingleObject(launched.process, 0) == win32con.WAIT_TIMEOUT


def stop_process(launched) -> None:
    """Ends the launched process and every process it started."""
    import win32job

    try:
        win32job.TerminateJobObject(launched.job, 0)
    except Exception:
        LOGGER.exception("Could not end a process the service started")
    try:
        launched.job.Close()
    except Exception:
        pass


# -- The signed-in standard user's own Beamer --------------------------------------------------

class UserStart:
    """Starts the named standard account's own Beamer once each time it signs in to the console, as
    that account and unelevated, as ShareMouse's service does: a Beamer copied in by hand has no
    sign-in task of its own. Once per sign-in, so a Beamer the user quits stays quit. Waits for
    the desktop (Explorer) to be there, and skips a sign-in where Beamer already runs."""

    def __init__(self, session, user_of, wanted_user, ready, running, launch):
        self._session, self._user_of, self._wanted_user = session, user_of, wanted_user
        self._ready, self._running, self._launch = ready, running, launch
        self._done_for = None

    def check(self) -> None:
        session = self._session()
        user = self._user_of(session) if session not in (None, NO_SESSION, 0) else None
        wanted = self._wanted_user()
        if not user or not wanted or user != wanted:
            self._done_for = None
            return
        if self._done_for == (session, user) or not self._ready(session):
            return
        self._done_for = (session, user)
        if self._running(session):
            return
        try:
            self._launch(session)
            LOGGER.info("Started the signed-in user's Beamer in session %s", session)
        except Exception:
            LOGGER.exception("Could not start the signed-in user's Beamer in session %s", session)


def session_user_sid(session: int) -> Optional[str]:
    import win32security
    import win32ts

    try:
        name = win32ts.WTSQuerySessionInformation(None, session, win32ts.WTSUserName)
        if not name:
            return None
        domain = win32ts.WTSQuerySessionInformation(None, session, win32ts.WTSDomainName)
        return win32security.ConvertSidToStringSid(win32security.LookupAccountName(domain or None, name)[0])
    except Exception:
        LOGGER.exception("Could not tell who is signed in to session %s", session)
        return None


def _processes_in(session: int) -> list:
    import win32ts

    return [str(name).lower() for in_session, _pid, name, _sid in win32ts.WTSEnumerateProcesses(None, 1, 0)
            if in_session == session]


def desktop_ready(session: int) -> bool:
    return "explorer.exe" in _processes_in(session)


def beamer_running(session: int) -> bool:
    return "beamer.exe" in _processes_in(session)


def launch_user_beamer(session: int) -> None:
    """The installer's allowed Beamer.exe as the session's own user, with that user's token and
    environment, on its default desktop: exactly what signing in and starting it would run."""
    import win32con
    import win32process
    import win32profile
    import win32ts

    import secure_helper

    exe = secure_helper.allowed_client()
    if not exe or not os.path.isfile(exe):
        raise OSError(f"no allowed Beamer.exe ({exe})")
    token = win32ts.WTSQueryUserToken(session)
    try:
        environment = win32profile.CreateEnvironmentBlock(token, False)
        startup = win32process.STARTUPINFO()
        startup.lpDesktop = "winsta0\\default"
        process, thread, _pid, _tid = win32process.CreateProcessAsUser(
            token, exe, f'"{exe}"', None, None, False, win32process.CREATE_UNICODE_ENVIRONMENT,
            environment, os.path.dirname(exe), startup)
        process.Close()
        thread.Close()
    finally:
        token.Close()


def log_folder() -> Optional[str]:
    """The logs folder beside this exe, when it is a plain folder (not a link or junction) under
    the protected Program Files; else None, and nothing is logged to a file."""
    import stat

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    program_files = os.path.abspath(os.environ.get("ProgramFiles", r"C:\Program Files"))
    if not getattr(sys, "frozen", False) or os.path.commonpath([exe_dir.lower(), program_files.lower()]) != program_files.lower():
        return None
    folder = os.path.join(exe_dir, LOG_FOLDER_NAME)
    try:
        os.makedirs(folder, exist_ok=True)
        info = os.lstat(folder)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return None
    return folder


def configure_logging(name: str) -> None:
    from logging.handlers import RotatingFileHandler

    folder = log_folder()
    if folder is None:
        return
    try:
        handler = RotatingFileHandler(os.path.join(folder, name), maxBytes=1024 * 1024, backupCount=2,
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
            import logon_receiver

            supervisor = Supervisor(console_session, launch_helper, process_alive, stop_process)
            sign_in = Supervisor(console_session, launch_logon_receiver, process_alive, stop_process,
                                 wanted=lambda session: logon_receiver.installed() and not signed_in(session),
                                 name="Sign-in screen receiver")
            import secure_helper

            user_start = UserStart(console_session, session_user_sid, secure_helper.standard_user,
                                   desktop_ready, beamer_running, launch_user_beamer)
            sas = sas_event()
            waits = [self.stop_event] + ([sas] if sas is not None else [])
            LOGGER.info("%s running", SERVICE_NAME)
            supervisor.check()
            sign_in.check()
            user_start.check()
            while True:
                woke = win32event.WaitForMultipleObjects(waits, False, int(CHECK_SECONDS * 1000))
                if woke == win32event.WAIT_OBJECT_0:
                    break
                if woke == win32event.WAIT_OBJECT_0 + 1:
                    send_sas()
                    continue
                supervisor.check()
                sign_in.check()
                user_start.check()
            sign_in.shutdown()
            supervisor.shutdown()
            LOGGER.info("%s stopped", SERVICE_NAME)

    configure_logging("secure-service.log")
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(Service)
    servicemanager.StartServiceCtrlDispatcher()
