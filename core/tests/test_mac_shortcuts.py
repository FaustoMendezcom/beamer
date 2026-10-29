"""core/mac_shortcuts.py: Command from a Mac on Same positions is the Windows key, except that
Command-C, -V and -X copy, paste and cut as Control-C, -V and -X."""

import unittest

from core.mac_shortcuts import CommandShortcuts


class CommandShortcutsTests(unittest.TestCase):
    def setUp(self):
        self.keys = CommandShortcuts()

    def run_keys(self, *events):
        out = []
        for name, down in events:
            out += self.keys.key(name, down)
        return out

    def test_command_c_copies_as_control_c(self):
        self.assertEqual(self.run_keys(("cmd", True), ("c", True), ("c", False), ("cmd", False)),
                         [("ctrl", True), ("c", True), ("c", False), ("ctrl", False)])

    def test_paste_twice_while_command_stays_down_stays_control(self):
        self.assertEqual(self.run_keys(("cmd", True), ("v", True), ("v", False), ("v", True), ("v", False),
                                       ("cmd", False)),
                         [("ctrl", True), ("v", True), ("v", False), ("v", True), ("v", False), ("ctrl", False)])

    def test_any_other_chord_is_the_windows_key(self):
        self.assertEqual(self.run_keys(("cmd", True), ("e", True), ("e", False), ("cmd", False)),
                         [("cmd", True), ("e", True), ("e", False), ("cmd", False)])

    def test_command_alone_is_a_windows_key_tap(self):
        self.assertEqual(self.run_keys(("cmd", True), ("cmd", True), ("cmd", False)),
                         [("cmd", True), ("cmd", False)])

    def test_the_right_command_key_and_shift_pass_through(self):
        self.assertEqual(self.run_keys(("shift", True), ("cmd_r", True), ("x", True), ("x", False),
                                       ("cmd_r", False), ("shift", False)),
                         [("shift", True), ("ctrl", True), ("x", True), ("x", False), ("ctrl", False),
                          ("shift", False)])

    def test_a_mouse_button_with_command_held_makes_it_the_windows_key(self):
        self.keys.key("cmd", True)
        self.assertEqual(self.keys.flush(), [("cmd", True)])
        self.assertEqual(self.keys.flush(), [])
        self.assertEqual(self.run_keys(("c", True), ("cmd", False)), [("c", True), ("cmd", False)])

    def test_other_keys_without_command_are_untouched(self):
        self.assertEqual(self.run_keys(("ctrl", True), ("c", True), ("c", False), ("ctrl", False)),
                         [("ctrl", True), ("c", True), ("c", False), ("ctrl", False)])

    def test_reset_forgets_a_held_back_command(self):
        self.keys.key("cmd", True)
        self.keys.reset()
        self.assertEqual(self.run_keys(("c", True)), [("c", True)])


if __name__ == "__main__":
    unittest.main()
