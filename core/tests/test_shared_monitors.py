"""core/shared_monitors.py and the `monitors` message: monitors wired to two machines' inputs,
the Switch computer chord, and the picture while shared monitors show the other machine."""

import time
import unittest

from core import crossing_state, protocol, shared_monitors, ways
from core.tests.responder_harness import B, CAPS, Machine, entry, wait_for
from core.tests.test_link import Side

PC1 = protocol.id_text(bytes(range(1, 17)))
PC2 = protocol.id_text(bytes(range(20, 36)))
MAC = protocol.id_text(bytes(range(40, 56)))
SCREEN = {"x": 0, "y": 0, "w": 3440, "h": 1440}
# Display 2 top-right on its own; 0 and 1 the shared pair below it, 1 on the left.
PC1_DISPLAYS = {"0": {"x": 0, "y": 1440, "w": 3440, "h": 1440},
                "1": {"x": -3440, "y": 1440, "w": 3440, "h": 1440},
                "2": {"x": 0, "y": 0, "w": 3440, "h": 1440}}


def announced(*ids, code=8):
    return [{"id": ident, "code": code, "display": str(index), "showing": False} for index, ident in enumerate(ids)]


class MonitorsMessageTests(unittest.TestCase):
    def test_each_action_round_trips_on_wire_6(self):
        listed = announced("GBT3403:A", "GBT3403:B")
        self.assertEqual(protocol.read_monitors(protocol.monitors_msg("announce", listed)), ("announce", listed, False))
        self.assertEqual(protocol.read_monitors(protocol.monitors_msg("announce", listed, host=True))[2], True)
        self.assertEqual(protocol.read_monitors(protocol.monitors_msg("give")), ("give", [], False))
        self.assertEqual(protocol.read_monitors(protocol.monitors_msg("request")), ("request", [], False))
        older = {"type": "monitors", "data": {"action": "announce", "monitors": listed}}
        self.assertEqual(protocol.read_monitors(older)[2], False)
        self.assertIsNone(protocol.read_monitors({"type": "monitors", "data": {"action": "announce",
                                                                               "monitors": listed, "host": 1}}))
        self.assertEqual(protocol.LINK_VERSION, 6)

    def test_rejects_malformed_monitors(self):
        good = {"id": "GBT3403:A", "code": 7, "display": "0", "showing": True}
        for monitors in ([{**good, "id": "bad id"}], [{**good, "code": 0}], [{**good, "code": 256}],
                         [{**good, "code": True}], [{**good, "display": ""}], [{**good, "showing": 1}],
                         [good, good], [dict(good, id=f"M:{n}") for n in range(17)], {"id": "x"}):
            with self.subTest(monitors=monitors):
                message = {"type": "monitors", "data": {"action": "announce", "monitors": monitors}}
                self.assertIsNone(protocol.read_monitors(message))
        for message in (None, {}, {"type": "monitors", "data": {"action": "take"}},
                        {"type": "settings", "data": {"action": "give"}}):
            self.assertIsNone(protocol.read_monitors(message))

    def test_an_older_peer_receives_nothing(self):
        link = Side().link()
        link._live = True
        link._caps = frozenset(CAPS)
        self.assertTrue(link.post(protocol.monitors_msg("give")))
        self.assertEqual(list(link._outbox), [])

    def test_the_responder_hands_a_capable_peers_message_to_the_app(self):
        caps = tuple(CAPS) + ("monitors",)
        seen = []
        machine = Machine([entry(B, "Peer")], caps=caps,
                          monitors_callback=lambda peer, read: seen.append((peer, read))).start()
        self.addCleanup(machine.stop)
        side = Side(machine)
        identity = side.identity
        side.identity = lambda: identity(caps=list(caps))
        link = side.link()
        self.addCleanup(link.stop)
        link.start()
        self.assertTrue(wait_for(lambda: bool(side.ups)))
        self.assertTrue(link.post(protocol.monitors_msg("request")))
        self.assertTrue(wait_for(lambda: bool(seen)))
        self.assertEqual(seen, [(B, ("request", [], False))])
        self.assertIsNone(machine.responder.owner)


class ChordWatchTests(unittest.TestCase):
    def setUp(self):
        self.fired = []
        self.watch = shared_monitors.ChordWatch(lambda: self.fired.append(1))
        self.watch.chord = "ctrl+alt+m"

    def press(self, *names):
        return [self.watch.feed(name, True) for name in names]

    def test_the_chord_fires_once_and_its_key_is_swallowed_down_and_up(self):
        self.assertEqual(self.press("ctrl", "alt", "m", "m"), [False, False, True, True])
        self.assertTrue(self.watch.feed("m", False))
        self.assertEqual(self.fired, [1])
        self.assertFalse(self.watch.feed("alt", False))

    def test_other_keys_and_the_key_alone_pass_through(self):
        self.assertEqual(self.press("m", "ctrl", "x"), [False, False, False])
        self.assertFalse(self.watch.feed("m", False))
        self.assertEqual(self.fired, [])

    def test_a_key_already_down_when_its_modifiers_arrive_is_a_repeat(self):
        self.assertEqual(self.press("m", "ctrl", "alt", "m"), [False, False, False, False])
        self.assertEqual(self.fired, [])

    def test_nothing_fires_while_recording_or_with_no_chord(self):
        self.watch.recording = True
        self.press("ctrl", "alt", "m")
        self.watch.feed("m", False)
        self.watch.recording, self.watch.chord = False, None
        self.press("m")
        self.assertEqual(self.fired, [])


class SharedWithTests(unittest.TestCase):
    def test_a_monitor_both_machines_have_is_shared_and_one_another_announces_is_not(self):
        peers = {PC2: announced("G:A", "G:B"), MAC: announced("G:C")}
        self.assertEqual({peer: [m["id"] for m in listed]
                          for peer, listed in shared_monitors.shared_with({"G:A", "G:B"}, peers).items()},
                         {PC2: ["G:A", "G:B"]})

    def test_a_monitor_two_peers_announce_goes_to_neither(self):
        peers = {PC2: announced("G:A"), MAC: announced("G:A")}
        self.assertEqual(shared_monitors.shared_with({"G:A"}, peers), {})

    def test_state_keeps_only_well_formed_values(self):
        held = shared_monitors.state({"shared_monitors": {
            "codes": {"G:A": 7, "G:B": 0, "G:C": "7"}, "key": "not a chord", "away": {"0": PC2},
            "peers": {PC2: [{"id": "G:A"}], MAC: "x"}}})
        self.assertEqual(held["codes"], {"G:A": 7})
        self.assertEqual(held["key"], "")
        self.assertEqual(set(held["peers"]), {PC2})
        self.assertIsNone(held["home"])


class PictureTests(unittest.TestCase):
    """The picture follows the shared pair: while it shows PC2, PC2's tile takes its place, so the
    Mac's and PC1's edges lead there with no change on the Mac."""

    def settings(self):
        peer = {"send": True, "allow_drive": True, "side": "", "side_set_at": 0, "side_by": "", "paired_with": [],
                "paired_at": 1, "linked": True, "from_1_4": False, "host": "10.9.9.9", "port": 24820, "hw": ""}
        return {"machine_id": PC1, "shortcut": True, "zones": [],
                "peers": [{**peer, "id": MAC, "name": "Mac", "platform": "macos", "token": "k1"},
                          {**peer, "id": PC2, "name": "PC2", "platform": "windows", "token": "k2"}],
                "layout": {PC1: {"x": 2, "y": -90, "w": 430, "h": 180, "displays": PC1_DISPLAYS},
                           MAC: {"x": 2, "y": -90, "w": 215, "h": 90, "displays": {"0": SCREEN}},
                           PC2: {"x": 2, "y": 130, "w": 430, "h": 90,
                                 "displays": {"0": SCREEN, "1": {**SCREEN, "x": 3440}}}},
                "layout_set_at": 100, "layout_by": PC1}

    def zones(self, settings):
        return sorted((zone["peer"], zone["kind"], zone.get("display"), zone.get("side"))
                      for zone in settings["zones"] if zone["kind"] == "edge")

    def test_away_gives_the_shared_place_to_the_peer_and_home_takes_it_back(self):
        held = self.settings()
        away = crossing_state.shared_monitors_moved(held, platform="pc", peer=PC2, away_keys=["0", "1"],
                                                   own_displays=None, now=200)
        saved = away["settings"]
        self.assertTrue(away["send_picture"])
        self.assertEqual({key: saved["layout"][PC1][key] for key in "xywh"}, {"x": 217, "y": -90, "w": 215, "h": 90})
        self.assertEqual(set(saved["layout"][PC1]["displays"]), {"2"})
        self.assertEqual({key: saved["layout"][PC2][key] for key in "xywh"}, {"x": 2, "y": 0, "w": 430, "h": 90})
        self.assertEqual(self.zones(saved), [(PC2, "edge", "2", "bottom"), (MAC, "edge", "2", "left")])
        self.assertGreater(saved["layout_set_at"], 100)
        # The Mac's own view of the same picture: down to PC2, right to PC1.
        mac_sees = {(contact[0], ident) for ident in (PC1, PC2)
                    for _s, contact, _t in ways.display_contacts(saved["layout"][MAC], saved["layout"][ident])
                    if contact[0] in ways.EDGES}
        self.assertEqual(mac_sees, {("right", PC1), ("bottom", PC2)})

        home = crossing_state.shared_monitors_moved(saved, platform="pc", peer=PC2, away_keys=[],
                                                   own_displays=PC1_DISPLAYS, now=300)
        back = home["settings"]
        self.assertEqual({key: back["layout"][PC1][key] for key in "xywh"}, {"x": 2, "y": -90, "w": 430, "h": 180})
        self.assertEqual(back["layout"][PC1]["displays"], PC1_DISPLAYS)
        self.assertEqual(self.zones(back), [(MAC, "edge", "1", "top"), (MAC, "edge", "2", "left")])
        self.assertFalse(ways.display_contacts(back["layout"][PC1], back["layout"][PC2]))
        self.assertNotIn("home", back["shared_monitors"])

    def test_coming_home_stores_the_apps_rects_as_the_picture_keeps_displays(self):
        from core.return_edge import Rect
        away = crossing_state.shared_monitors_moved(self.settings(), platform="pc", peer=PC2, away_keys=["0", "1"],
                                                   own_displays=None, now=200)["settings"]
        rects = {key: Rect(rect["x"], rect["y"], rect["w"], rect["h"]) for key, rect in PC1_DISPLAYS.items()}
        home = crossing_state.shared_monitors_moved(away, platform="pc", peer=PC2, away_keys=[],
                                                   own_displays=rects, now=300)
        self.assertTrue(home["changed"])
        self.assertEqual(home["settings"]["layout"][PC1]["displays"], PC1_DISPLAYS)

    def test_going_away_twice_keeps_the_first_home(self):
        held = self.settings()
        once = crossing_state.shared_monitors_moved(held, platform="pc", peer=PC2, away_keys=["0", "1"],
                                                   own_displays=None, now=200)["settings"]
        twice = crossing_state.shared_monitors_moved(once, platform="pc", peer=PC2, away_keys=["0", "1"],
                                                    own_displays=None, now=300)
        self.assertFalse(twice["changed"])
        self.assertEqual(twice["settings"]["shared_monitors"]["home"]["w"], 430)

    def test_nothing_moves_when_every_display_would_go(self):
        held = self.settings()
        out = crossing_state.shared_monitors_moved(held, platform="pc", peer=PC2, away_keys=["0", "1", "2"],
                                                  own_displays=None, now=200)
        self.assertFalse(out["send_picture"])
        self.assertEqual(out["settings"]["layout"], held["layout"])

    def test_park_leaves_the_peer_touching_nothing(self):
        layout = self.settings()["layout"]
        layout[PC2] = {**layout[PC2], "y": 90}
        parked = shared_monitors.park(layout, PC2)
        self.assertEqual(parked["y"], 90 + shared_monitors.PARK_GAP)
        self.assertFalse(any(ways._overlaps(parked, other) for key, other in layout.items() if key != PC2))


if __name__ == "__main__":
    unittest.main()
