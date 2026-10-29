"""Dragging files across an edge: where the catcher lies on the sending side, the landing on the
receiving side, and Send to's hand-off to the running Beamer."""

import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.return_edge import Rect  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication

    import drag_catch_win
    import drop_landing_win
    import kvm_bridge_win
except ImportError:  # PySide6 is only in the Windows venv
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class EdgeStripTests(unittest.TestCase):
    def test_each_side_lies_along_its_outermost_pixels_over_the_contact(self):
        screen = Rect(0, 0, 3440, 1440)
        self.assertEqual(drag_catch_win.edge_strip(screen, "left", [0.0, 1.0]), (0, 0, 3, 1440))
        self.assertEqual(drag_catch_win.edge_strip(screen, "right", [0.5, 1.0]), (3437, 720, 3, 720))
        self.assertEqual(drag_catch_win.edge_strip(screen, "bottom", [0.0, 0.5]), (0, 1437, 1720, 3))
        self.assertEqual(drag_catch_win.edge_strip(Rect(-3440, 1440, 3440, 1440), "top", None), (-3440, 1440, 3440, 3))
        self.assertIsNone(drag_catch_win.edge_strip(screen, "corner", None))

    def test_only_a_pointer_near_the_strip_wants_it(self):
        strip = (3437, 0, 3, 1440)
        self.assertTrue(drag_catch_win.near(strip, (3400, 700)))
        self.assertFalse(drag_catch_win.near(strip, (1000, 700)))

    def test_only_zones_to_peers_that_take_files_are_caught(self):
        app = QApplication.instance() or QApplication([])
        zones = [{"peer": "PC2", "kind": "edge", "display": "2", "side": "bottom", "contact_span": [0.0, 1.0]},
                 {"peer": "MAC", "kind": "edge", "display": "2", "side": "left", "contact_span": [0.0, 1.0]}]
        catcher = drag_catch_win.DragCatcher(lambda: zones, lambda: {"2": Rect(0, 0, 3440, 1440)},
                                             lambda: (100, 1400), lambda: {"PC2"}, lambda peer, paths: None)
        wanted = catcher._wanted((100, 1400))
        self.assertEqual(list(wanted), [("PC2", "2", "bottom")])
        self.assertEqual(catcher._wanted((1700, 700)), {})
        del app


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class DropLandingTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.landed = []
        drop_landing_win.user32 = None
        self.addCleanup(setattr, drop_landing_win, "user32", drop_landing_win.user32)

    def landing(self, folder):
        return drop_landing_win.DropLanding(lambda: (10, 20), lambda x, y: folder, lambda: "C:/Downloads/Beamer",
                                            lambda incoming, where: self.landed.append((incoming["label"], where)))

    def test_letting_go_over_a_folder_lands_there(self):
        landing = self.landing("C:/Users/me/Desktop")
        landing.arm({"label": "photo.jpg"})
        self.assertTrue(landing.armed)
        landing.released()
        self.assertEqual(self.landed, [("photo.jpg", "C:/Users/me/Desktop")])
        self.assertFalse(landing.armed)

    def test_letting_go_anywhere_else_goes_to_the_received_folder_and_twice_lands_once(self):
        landing = self.landing(None)
        landing.arm({"label": "a.txt"})
        landing.released()
        landing.released()
        self.assertEqual(self.landed, [("a.txt", "C:/Downloads/Beamer")])

    def test_not_letting_go_in_time_still_lands_in_the_received_folder(self):
        landing = self.landing("C:/Somewhere")
        landing.arm({"label": "a.txt"})
        landing._until = 0
        landing._tick()
        self.assertEqual(self.landed, [("a.txt", "C:/Downloads/Beamer")])


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class SendToHandOffTests(unittest.TestCase):
    def read(self, request):
        sent, shown = [], []
        signal = SimpleNamespace(send=sent.append, activate=shown.append)
        line = (json.dumps(request) + "\n").encode("utf-8")
        connection = SimpleNamespace(canReadLine=lambda: True, readLine=lambda: line, disconnectFromServer=lambda: None)
        kvm_bridge_win.InstanceSignal._read(signal, connection)
        return sent, shown

    def test_a_send_request_hands_over_its_paths(self):
        self.assertEqual(self.read({"send": ["C:/a.txt", 5, "C:/b"]}), ([["C:/a.txt", "C:/b"]], []))

    def test_an_activation_still_shows_the_window(self):
        self.assertEqual(self.read({"activation_token": ""}), ([], [None]))


if __name__ == "__main__":
    unittest.main()
