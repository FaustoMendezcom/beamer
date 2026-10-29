"""shared_monitors_win.SharedMonitors against a fake DDC/CI and desktop: learning this PC's own
input codes, telling which machine each shared monitor shows, and switching them."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import shared_monitors_win  # noqa: E402
from core.return_edge import Rect  # noqa: E402
from monitors_ddc import Monitor, edid_identity  # noqa: E402

PC2 = "FBQWFxgZGhscHR4fICEiIw"
TOP = Monitor("2", r"\\.\DISPLAY5", Rect(0, 0, 3440, 1440), None)
RIGHT = Monitor("0", r"\\.\DISPLAY17", Rect(0, 1440, 3440, 1440), "GBT3403:972")
LEFT = Monitor("1", r"\\.\DISPLAY18", Rect(-3440, 1440, 3440, 1440), "GBT3403:802")


class FakeDdc:
    """Monitors that answer only while they show this PC (input 7); 8 is the other PC's."""

    def __init__(self):
        self.inputs = {RIGHT.device: 7, LEFT.device: 7, TOP.device: 5}
        self.sets = []
        self.refuse = set()

    def monitors(self):
        return [RIGHT, LEFT, TOP]

    def read_input(self, device):
        value = self.inputs[device]
        return value if value in (5, 7) else None

    def set_input(self, device, code):
        self.sets.append((device, code))
        if device in self.refuse or self.read_input(device) is None:
            return False
        self.inputs[device] = code
        return True


class FakeDesktop:
    def __init__(self):
        self.away = []
        self.fences = 0

    def set_away(self, rects):
        self.away = list(rects)

    def fence(self):
        self.fences += 1


class SharedMonitorsTests(unittest.TestCase):
    def setUp(self):
        self.ddc, self.desktop = FakeDdc(), FakeDesktop()
        self.changes, self.learned = [], []
        self.monitors = shared_monitors_win.SharedMonitors(
            self.ddc, self.desktop, codes={}, peers={},
            changed=lambda: self.changes.append(1), learned=self.learned.append)
        self.original_timer = shared_monitors_win.threading.Timer
        shared_monitors_win.threading.Timer = lambda *_args, **_kwargs: type("T", (), {"start": lambda self: None})()
        self.addCleanup(setattr, shared_monitors_win.threading, "Timer", self.original_timer)

    def announce(self, showing=False):
        self.monitors.set_peer(PC2, [{"id": "GBT3403:972", "code": 8, "display": "0", "showing": showing},
                                     {"id": "GBT3403:802", "code": 8, "display": "1", "showing": showing}])

    def test_reading_its_own_input_learns_the_code_and_only_identified_monitors_count(self):
        self.assertTrue(self.monitors.poll())
        self.assertEqual(self.learned, [{"GBT3403:972": 7, "GBT3403:802": 7}])
        self.assertEqual(sorted(m["id"] for m in self.monitors.announcement()), ["GBT3403:802", "GBT3403:972"])
        self.assertTrue(all(m["showing"] and m["code"] == 7 for m in self.monitors.announcement()))
        self.assertEqual(self.monitors.shared(), {})

    def test_a_peer_announcing_the_same_monitors_shares_them(self):
        self.monitors.poll()
        self.announce()
        self.assertEqual(sorted(m["id"] for m in self.monitors.shared()[PC2]), ["GBT3403:802", "GBT3403:972"])
        self.assertEqual(self.monitors.away(), {})
        self.assertEqual(len(self.monitors.showing_here(PC2)), 2)

    def test_give_switches_them_to_the_peers_input_and_hides_them(self):
        self.monitors.poll()
        self.announce()
        self.assertEqual(sorted(self.monitors.give(PC2)), ["0", "1"])
        self.assertEqual(sorted(self.ddc.sets), sorted([(LEFT.device, 8), (RIGHT.device, 8)]))
        self.assertEqual(self.monitors.away(), {"0": PC2, "1": PC2})
        self.assertEqual(sorted(self.desktop.away, key=lambda rect: rect.x), [LEFT.rect, RIGHT.rect])
        # Once away, reading confirms it; the codes it learned stay.
        self.monitors.poll()
        self.assertEqual(self.monitors.away(), {"0": PC2, "1": PC2})
        self.assertEqual(self.monitors.snapshot()["codes"], {"GBT3403:972": 7, "GBT3403:802": 7})

    def test_monitors_that_come_back_are_shown_again(self):
        self.monitors.poll()
        self.announce()
        self.monitors.give(PC2)
        self.ddc.inputs[RIGHT.device] = self.ddc.inputs[LEFT.device] = 7
        self.assertTrue(self.monitors.poll())
        self.assertEqual(self.monitors.away(), {})
        self.assertEqual(self.desktop.away, [])

    def test_monitors_asleep_answer_neither_machine_and_stay(self):
        self.monitors.poll()
        self.announce(showing=False)
        self.ddc.inputs[RIGHT.device] = self.ddc.inputs[LEFT.device] = 0
        self.monitors.poll()
        self.assertEqual(self.monitors.away(), {})
        self.assertEqual(self.desktop.away, [])

    def test_away_needs_the_peer_to_say_it_shows_them(self):
        self.monitors.poll()
        self.announce(showing=False)
        self.ddc.inputs[RIGHT.device] = self.ddc.inputs[LEFT.device] = 8
        self.monitors.poll()
        self.assertEqual(self.monitors.away(), {})
        self.announce(showing=True)
        self.assertEqual(self.monitors.away(), {"0": PC2, "1": PC2})

    def test_a_peer_saying_it_does_not_show_them_after_a_give_brings_them_back(self):
        self.monitors.poll()
        self.announce(showing=False)
        self.monitors.give(PC2)
        self.assertEqual(self.monitors.away(), {"0": PC2, "1": PC2})
        self.announce(showing=False)
        self.assertEqual(self.monitors.away(), {})

    def test_a_peers_give_shows_them_here_at_once(self):
        self.monitors.poll()
        self.announce()
        self.monitors.give(PC2)
        self.monitors.taken(PC2)
        self.assertEqual(self.monitors.away(), {})
        self.assertEqual(self.desktop.away, [])
        self.assertEqual(len(self.monitors.showing_here(PC2)), 2)

    def test_give_with_nothing_showing_here_switches_nothing(self):
        self.ddc.inputs[RIGHT.device] = self.ddc.inputs[LEFT.device] = 8
        self.monitors.poll()
        self.announce()
        self.assertEqual(self.monitors.give(PC2), [])
        self.assertEqual(self.ddc.sets, [])

    def test_a_monitor_the_peer_does_not_share_never_hides(self):
        self.ddc.inputs[RIGHT.device] = 8
        self.monitors.poll()
        self.monitors.set_peer(PC2, [{"id": "GBT3403:802", "code": 8, "display": "1", "showing": False}])
        self.assertEqual(self.monitors.away(), {})

    def test_only_the_host_with_a_display_of_its_own_moves_the_picture(self):
        self.monitors.poll()
        self.announce()
        self.assertFalse(self.monitors.moves_picture())
        self.monitors.host = True
        self.assertTrue(self.monitors.moves_picture())
        self.assertEqual(self.monitors.shared_keys(), {"0", "1"})
        self.assertTrue(self.monitors.has_own_display())

    def test_two_host_claims_go_to_the_earlier_then_the_lower_id(self):
        self.monitors.poll()
        self.monitors.host, self.monitors.host_since, self.monitors._own_id = True, 100, "zzzz"
        listed = [{"id": "GBT3403:972", "code": 8, "display": "0", "showing": False}]
        self.monitors.set_peer(PC2, listed, host=True, host_since=200)
        self.assertTrue(self.monitors.peer_hosts())
        self.assertTrue(self.monitors.moves_picture())
        self.monitors.set_peer(PC2, listed, host=True, host_since=50)
        self.assertFalse(self.monitors.moves_picture())
        self.monitors.set_peer(PC2, listed, host=True, host_since=100)
        self.assertFalse(self.monitors.moves_picture())
        self.monitors._own_id = "AAAA"
        self.assertTrue(self.monitors.moves_picture())
        only = shared_monitors_win.SharedMonitors(
            type("Ddc", (FakeDdc,), {"monitors": lambda self: [RIGHT, LEFT]})(), self.desktop,
            codes={}, peers={}, changed=lambda: None, learned=lambda codes: None)
        only.host = True
        only.poll()
        only.set_peer(PC2, [{"id": "GBT3403:972", "code": 7, "display": "0", "showing": False},
                            {"id": "GBT3403:802", "code": 7, "display": "1", "showing": False}])
        self.assertFalse(only.moves_picture())

    def test_never_hides_every_display(self):
        only = shared_monitors_win.SharedMonitors(
            type("Ddc", (FakeDdc,), {"monitors": lambda self: [RIGHT, LEFT]})(), self.desktop,
            codes={}, peers={}, changed=lambda: None, learned=lambda codes: None)
        only._ddc.inputs[RIGHT.device] = only._ddc.inputs[LEFT.device] = 8
        only.poll()
        only.set_peer(PC2, [{"id": "GBT3403:972", "code": 8, "display": "0", "showing": True},
                            {"id": "GBT3403:802", "code": 8, "display": "1", "showing": True}])
        self.assertEqual(only.away(), {})


class GuestPictureTests(unittest.TestCase):
    """A PC that is not host puts the shared monitors in the picture only while it shows them,
    and its own other screen (a laptop's) never in their place."""

    LAPTOP = Rect(3440, -170, 2560, 1600)

    def displays(self, away):
        import kvm_bridge_win

        desktop = type("Desktop", (), {"display_map_all": staticmethod(
            lambda: {"0": RIGHT.rect, "1": LEFT.rect, "2": self.LAPTOP})})()
        monitors = type("Monitors", (), {"shared_keys": lambda self: {"0", "1"}, "away": lambda self: away})()
        return kvm_bridge_win.WindowsApplication._guest_display_map(desktop, monitors)

    def test_showing_them_reports_just_the_shared_ones(self):
        self.assertEqual(sorted(self.displays({})), ["0", "1"])

    def test_not_showing_them_reports_its_own_screen(self):
        self.assertEqual(self.displays({"0": "host", "1": "host"}), {"2": self.LAPTOP})


class EdidTests(unittest.TestCase):
    def edid(self, serial_text=b"24142B000972", number=0):
        block = bytearray(128)
        block[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
        maker = ((ord("G") - 64) << 10) | ((ord("B") - 64) << 5) | (ord("T") - 64)
        block[8:10] = maker.to_bytes(2, "big")
        block[10:12] = (0x3403).to_bytes(2, "little")
        block[12:16] = number.to_bytes(4, "little")
        if serial_text:
            block[54:59] = b"\x00\x00\x00\xff\x00"
            block[59:72] = serial_text.ljust(13, b"\x0a")[:13]
        return bytes(block)

    def test_maker_product_and_serial_text(self):
        self.assertEqual(edid_identity(self.edid()), "GBT3403:24142B000972")

    def test_numeric_serial_when_there_is_no_text(self):
        self.assertEqual(edid_identity(self.edid(b"", 1234)), "GBT3403:1234")

    def test_no_serial_or_a_bad_block_is_no_identity(self):
        self.assertIsNone(edid_identity(self.edid(b"", 0)))
        self.assertIsNone(edid_identity(self.edid(b"0")))
        self.assertIsNone(edid_identity(b"\x00" * 128))
        self.assertIsNone(edid_identity(None))


if __name__ == "__main__":
    unittest.main()
