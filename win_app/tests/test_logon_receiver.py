"""The sign-in screen receiver (logon_receiver) and when the service runs it."""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import logon_receiver  # noqa: E402
import secure_service  # noqa: E402
from core import protocol  # noqa: E402

MACHINE = protocol.id_text(bytes(range(16)))
SETTINGS = {"machine_id": MACHINE, "name": "COR", "port": 24820,
            "peers": [{"id": protocol.id_text(bytes(range(16, 32))), "token": "t", "name": "Mac"}],
            "zones": [{"edge": "left"}]}


class SettingsTests(unittest.TestCase):
    def test_beside_the_service_exe(self):
        self.assertEqual(logon_receiver.settings_path(r"C:\Program Files\Beamer\BeamerSecure.exe"),
                         r"C:\Program Files\Beamer\logon\settings.json")

    def test_the_same_pc_its_own_beamer_says_with_no_optional_messages(self):
        own = logon_receiver.identity(SETTINGS, "1.5.18")
        self.assertEqual(own["id"], protocol.read_id(MACHINE))
        self.assertEqual((own["name"], own["platform"], own["app"], own["caps"], own["port"]),
                         ("COR", "windows", "1.5.18", [], 24820))

    def test_its_beacon_is_the_apps_with_no_code_up(self):
        from core import pairing

        sent = pairing.decode(logon_receiver.beacon(logon_receiver.identity(SETTINGS, "dev")))
        self.assertEqual({key: sent[key] for key in ("type", "name", "port", "pairing", "platform")},
                         {"type": "beacon", "name": "COR", "port": 24820, "pairing": 3, "platform": "windows"})
        self.assertNotIn("id", sent)
        self.assertNotIn("pair", sent)

    def test_no_machine_id_or_peers_is_refused(self):
        with self.assertRaises(ValueError):
            logon_receiver.identity(dict(SETTINGS, machine_id=""), "1.5.18")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"machine_id": MACHINE}, handle)
            with self.assertRaises(ValueError):
                logon_receiver.load_settings(path)

    def test_what_a_link_saves_stays_in_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(SETTINGS, handle)
            before = os.path.getmtime(path)
            book = logon_receiver.MemoryBook(logon_receiver.load_settings(path)).book
            book.store_paired(SETTINGS["peers"][0]["id"], ["x"])
            self.assertEqual(book.peers()[0]["paired_with"], ["x"])
            self.assertEqual(book.zones(), SETTINGS["zones"])
            self.assertEqual(os.path.getmtime(path), before)
            with open(path, encoding="utf-8") as handle:
                self.assertNotIn("paired_with", json.load(handle)["peers"][0])


class WhenItRunsTests(unittest.TestCase):
    def make(self, signed_in):
        self.session = 1
        self.launched, self.stopped = [], []
        self.signed = signed_in
        return secure_service.Supervisor(
            lambda: self.session, lambda session: self.launched.append(session) or f"p{session}",
            lambda process: True, self.stopped.append, wanted=lambda session: not self.signed[0],
            name="Sign-in screen receiver")

    def test_only_while_nobody_is_signed_in(self):
        signed = [False]
        supervisor = self.make(signed)
        supervisor.check()
        self.assertEqual(self.launched, [1])
        signed[0] = True
        supervisor.check()
        self.assertEqual(self.stopped, ["p1"])
        supervisor.check()
        self.assertEqual(self.launched, [1])
        signed[0] = False
        supervisor.check()
        self.assertEqual(self.launched, [1, 1])

    def test_never_in_session_zero_or_with_no_console(self):
        supervisor = self.make([False])
        for session in (0, secure_service.NO_SESSION):
            self.session = session
            supervisor.check()
        self.assertEqual(self.launched, [])

    def test_the_helper_is_always_wanted(self):
        launched = []
        supervisor = secure_service.Supervisor(lambda: 1, launched.append, lambda process: True, lambda p: None)
        supervisor.check()
        self.assertEqual(launched, [1])


class UserStartTests(unittest.TestCase):
    """The service starts the named standard account's Beamer once per sign-in, as that account."""

    WORKER = "S-1-5-21-1-2-3-1001"

    def make(self, user, wanted=WORKER, ready=True, running=False):
        self.state = {"user": user, "ready": ready, "running": running, "session": 1}
        self.launched = []
        return secure_service.UserStart(
            lambda: self.state["session"], lambda session: self.state["user"], lambda: wanted,
            lambda session: self.state["ready"], lambda session: self.state["running"], self.launched.append)

    def test_once_per_sign_in_after_the_desktop_is_up(self):
        start = self.make(None, ready=False)
        start.check()
        self.state["user"] = self.WORKER
        start.check()
        self.assertEqual(self.launched, [])
        self.state["ready"] = True
        start.check()
        start.check()
        self.assertEqual(self.launched, [1])
        # Quit by the user: not started again until the next sign-in.
        self.state["user"] = None
        start.check()
        self.state["user"] = self.WORKER
        start.check()
        self.assertEqual(self.launched, [1, 1])

    def test_not_for_another_account_none_named_or_beamer_already_running(self):
        for start in (self.make("S-1-5-21-1-2-3-500"), self.make(self.WORKER, wanted=None),
                      self.make(self.WORKER, running=True)):
            start.check()
        self.assertEqual(self.launched, [])


class StopsTheWholeTreeTests(unittest.TestCase):
    def test_stopping_ends_the_job_not_just_the_launcher(self):
        from unittest import mock

        ended, closed = [], []
        job = mock.Mock(Close=lambda: closed.append(1))
        with mock.patch.dict(sys.modules, {"win32job": mock.Mock(TerminateJobObject=lambda j, code: ended.append(j))}):
            secure_service.stop_process(secure_service.Launched(object(), job))
        self.assertEqual((ended, closed), ([job], [1]))


@unittest.skipUnless(sys.platform == "win32", "Windows")
class ListensTests(unittest.TestCase):
    def test_the_responder_runs_without_the_app(self):
        from core.receiver import LinkResponder

        book = logon_receiver.MemoryBook(SETTINGS).book
        server = LinkResponder(book, lambda: logon_receiver.identity(SETTINGS, "dev"),
                               status_callback=lambda state, detail: None, zones=book.zones)
        server._bind = lambda port, stop: __import__("socket").create_server(("127.0.0.1", 0))
        server.start(0)
        try:
            deadline = time.monotonic() + 5
            while not server.listening and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(server.listening)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
