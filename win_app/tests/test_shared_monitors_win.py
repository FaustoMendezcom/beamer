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
        self.assertFalse(self.monitors.poll())
        self.assertTrue(self.monitors.poll())
        self.assertEqual(self.monitors.away(), {})
        self.assertEqual(self.desktop.away, [])

    def test_a_monitor_answering_by_turns_moves_nothing(self):
        # The other PC shut down: its monitors hunt for a signal, answering one read and not the next.
        self.monitors.poll()
        self.announce(showing=True)
        self.assertEqual(self.monitors.away(), {})
        for value in (8, 7, 8, 7, 8, 7):
            self.ddc.inputs[RIGHT.device] = self.ddc.inputs[LEFT.device] = value
            self.monitors.poll()
            self.assertEqual(self.monitors.away(), {})
        self.ddc.inputs[RIGHT.device] = self.ddc.inputs[LEFT.device] = 8
        self.monitors.poll()
        self.monitors.poll()
        self.assertEqual(self.monitors.away(), {"0": PC2, "1": PC2})

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

    def test_a_peer_claim_without_its_time_yields_to_a_standing_one(self):
        self.monitors.poll()
        self.monitors.host, self.monitors.host_since, self.monitors._own_id = True, 1, "zzzz"
        self.monitors.set_peer(PC2, [{"id": "GBT3403:972", "code": 8, "display": "0", "showing": False}], host=True)
        self.assertFalse(self.monitors.host_contested())
        self.assertTrue(self.monitors.moves_picture())
        only = shared_monitors_win.SharedMonitors(
            type("Ddc", (FakeDdc,), {"monitors": lambda self: [RIGHT, LEFT]})(), self.desktop,
            codes={}, peers={}, changed=lambda: None, learned=lambda codes: None)
        only.host = True
        only.poll()
        only.set_peer(PC2, [{"id": "GBT3403:972", "code": 7, "display": "0", "showing": False},
                            {"id": "GBT3403:802", "code": 7, "display": "1", "showing": False}])
        self.assertFalse(only.moves_picture())

    def test_a_machine_paired_again_under_a_new_id_does_not_make_the_monitors_unshared(self):
        self.monitors.poll()
        self.announce()
        self.monitors.set_peer("NEWID", [{"id": "GBT3403:972", "code": 8, "display": "0", "showing": False},
                                         {"id": "GBT3403:802", "code": 8, "display": "1", "showing": False}])
        self.assertEqual(self.monitors.shared(), {})
        self.assertTrue(self.monitors.keep_peers({"NEWID"}))
        self.assertEqual(sorted(self.monitors.shared()), ["NEWID"])
        self.assertFalse(self.monitors.keep_peers({"NEWID"}))

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

    def test_learning_which_are_shared_reports_them_again_with_nothing_away(self):
        # A fresh start shares nothing until the peer's announcement arrives; that alone, with
        # nothing switched away, must put the displays in the picture again.
        import types

        import kvm_bridge_win

        shared = [set()]
        calls = []
        app = types.SimpleNamespace(
            shared_monitors=types.SimpleNamespace(
                announcement=lambda: [], away=lambda: {}, shared_keys=lambda: set(shared[0]),
                host=False, host_since=0),
            _closing=False, _monitors_announced=[], _monitors_away={}, _config=object(),
            _peer_caps=lambda: {}, _reflect_shared_monitors=lambda: None,
            _apply_monitors_away=lambda away: calls.append("away"),
            _refresh_own_displays=lambda: calls.append("refresh"),
            _reconcile_displays=lambda: calls.append("reconcile"))
        changed = kvm_bridge_win.WindowsApplication._on_shared_monitors_changed
        changed(app)
        self.assertEqual(calls, [])
        shared[0] = {"0", "1"}
        changed(app)
        self.assertEqual(calls, ["refresh", "reconcile"])
        changed(app)
        self.assertEqual(calls, ["refresh", "reconcile"])


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


class WakeBeforeSwitchTests(unittest.TestCase):
    """Monitors switched to a PC whose screens are off find no signal and come back, leaving the
    input there unseen: the screens are woken first, and the monitors and input go once on."""

    def app(self, idle):
        import types
        import kvm_bridge_win
        from unittest import mock

        sent, woken, given, passed = [], [], [], []
        app = types.SimpleNamespace(
            _send_to=lambda peer, message: sent.append(message["data"]["action"]),
            _label_of=lambda peer: "PC2", _waiting_awake=None, _closing=False,
            _on_alert=lambda *args: None, _pass_input_to=passed.append,
            shared_monitors=types.SimpleNamespace(give=lambda peer: given.append(peer) or ["0", "1"]))
        for name in ("_give_to", "_wake_for", "_give_again_if_bounced"):
            setattr(app, name, types.MethodType(getattr(kvm_bridge_win.WindowsApplication, name), app))
        desktop = types.SimpleNamespace(idle_seconds=lambda: idle, wake_displays=lambda: woken.append(1))
        patches = [mock.patch.object(kvm_bridge_win, "desktop_win", desktop),
                   mock.patch.object(kvm_bridge_win.QTimer, "singleShot", lambda ms, call: call() if ms != kvm_bridge_win.BOUNCE_CHECK_MS else None)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return app, sent, woken, given, passed

    def test_a_pc_in_use_answers_awake_at_once_without_waking(self):
        app, sent, woken, _given, _passed = self.app(idle=3)
        app._wake_for("RFhY")
        self.assertEqual((sent, woken), (["awake"], []))

    def test_an_idle_pc_wakes_its_screens_then_answers(self):
        app, sent, woken, _given, _passed = self.app(idle=600)
        app._wake_for("RFhY")
        self.assertEqual((sent, woken), (["awake"], [1]))

    def test_the_monitors_and_input_go_once_whether_awake_or_the_wait_comes_first(self):
        app, sent, _woken, given, passed = self.app(idle=0)
        app._waiting_awake = "RFhY"
        app._give_to("RFhY")
        app._give_to("RFhY")
        self.assertEqual((given, passed), (["RFhY"], ["RFhY"]))
        self.assertEqual(sent, ["give"])

    def test_nothing_goes_for_a_switch_not_waiting(self):
        app, sent, _woken, given, _passed = self.app(idle=0)
        app._give_to("RFhY")
        self.assertEqual((given, sent), ([], []))

if __name__ == "__main__":
    unittest.main()
