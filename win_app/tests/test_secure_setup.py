import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import secure_setup  # noqa: E402


class CommandTests(unittest.TestCase):
    def test_install_passes_this_beamer_and_elevated_windows(self):
        text = secure_setup.command(True, r"C:\Users\O'Neil\Beamer.exe", Path(r"C:\x\Install-SecureDesktop.ps1"), Path(r"C:\t\l.log"))
        self.assertIn(r"-ClientPath 'C:\Users\O''Neil\Beamer.exe' -ElevatedWindows", text)
        self.assertIn(r"& 'C:\x\Install-SecureDesktop.ps1'", text)
        self.assertIn("exit 1", text)

    def test_remove(self):
        text = secure_setup.command(False, "C:\b.exe", Path("C:\s.ps1"), Path("C:\l.log"))
        self.assertIn("-Remove", text)
        self.assertNotIn("-ClientPath", text)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.exe = os.path.join(self.folder, "Beamer.exe")
        self.copy = os.path.join(self.folder, "BeamerSecure.exe")
        for path in (self.exe, self.copy):
            Path(path).write_bytes(b"beamer")
            os.utime(path, (1_700_000_000, 1_700_000_000))

    def state(self, values, exists=True):
        with mock.patch.object(secure_setup.sys, "platform", "win32"), \
                mock.patch.object(secure_setup, "_service_exists", return_value=exists), \
                mock.patch.object(secure_setup, "_values", return_value=values):
            return secure_setup.state(self.exe)

    def test_not_installed(self):
        self.assertEqual(self.state({}, exists=False), secure_setup.State())

    def test_installed_for_this_beamer_and_current(self):
        got = self.state({"AllowedClient": self.exe.upper(), "ServiceExe": self.copy, "ElevatedWindows": "1"})
        self.assertEqual(got, secure_setup.State(True, True, True, False))

    def test_outdated_copy_and_other_client(self):
        os.utime(self.copy, (1_600_000_000, 1_600_000_000))
        got = self.state({"AllowedClient": r"C:\other\Beamer.exe", "ServiceExe": self.copy})
        self.assertEqual(got, secure_setup.State(True, False, False, True))

    def test_the_installer_is_found(self):
        self.assertTrue(secure_setup.script_path().is_file())


if __name__ == "__main__":
    unittest.main()


class SecureControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def make(self, current):
        from types import SimpleNamespace
        from PySide6.QtWidgets import QVBoxLayout, QWidget
        import kvm_bridge_win as kvm
        page = SimpleNamespace(_set_secure=mock.Mock())
        page._refresh_secure = lambda: kvm.WindowsApplication._refresh_secure(page)
        host = QWidget()
        self.addCleanup(host.deleteLater)
        with mock.patch.object(secure_setup, "state", return_value=current):
            kvm.WindowsApplication._secure_control(page, SimpleNamespace(body=QVBoxLayout(host)))
        return page

    def test_off_when_not_installed(self):
        page = self.make(secure_setup.State())
        self.assertFalse(page.secure_switch.isChecked())
        self.assertTrue(page.secure_switch.isEnabled())
        self.assertTrue(page.secure_update.isHidden())

    def test_on_and_offers_an_update_for_an_older_copy(self):
        page = self.make(secure_setup.State(True, True, True, True))
        self.assertTrue(page.secure_switch.isChecked())
        self.assertFalse(page.secure_update.isHidden())
        self.assertIn("older", page.secure_note.text())

    def test_installed_for_another_beamer_shows_off(self):
        page = self.make(secure_setup.State(True, False, True, False))
        self.assertFalse(page.secure_switch.isChecked())
        self.assertIn("another Beamer", page.secure_note.text())
