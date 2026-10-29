"""Mac shortcuts on a PC driven from a Mac with Same positions: Command arrives as the Windows key,
but Command-C, -V and -X still copy, paste and cut, as Control-C, -V and -X. Every other Command
chord stays a Windows-key chord, and Command alone still opens Start.

The Windows key is held back when Command goes down, until the next key says which it is: a
shortcut key makes it Control for as long as Command stays down; any other key, or a mouse
button, makes it the Windows key. Released alone, it is a Windows-key tap. Pure: the receiver
injects what `key` and `flush` return."""

from typing import List, Optional, Tuple

COMMANDS = frozenset({"cmd", "cmd_r"})
# Copy, paste and cut. Kept to these: Command-L, -D, -E and the rest stay Windows' own chords.
SHORTCUT_KEYS = frozenset({"c", "v", "x"})
CONTROL = "ctrl"

Event = Tuple[str, bool]


class CommandShortcuts:
    def __init__(self, keys=SHORTCUT_KEYS):
        self.keys = frozenset(keys)
        self.pending: Optional[str] = None   # the Command key held back, not yet injected
        self.mode: Optional[str] = None      # "ctrl" or "win" once the next key decided
        self.command: Optional[str] = None   # which Command key is down

    def key(self, name: str, down: bool) -> List[Event]:
        """The events to inject for this key in place of it."""
        if name in COMMANDS:
            if down:
                if self.command is not None:
                    return []                # an auto-repeat while undecided or decided
                self.command, self.pending = name, name
                return []
            if name != self.command:
                return [(name, False)]
            out = {"ctrl": [(CONTROL, False)], "win": [(name, False)]}.get(
                self.mode, [(name, True), (name, False)] if self.pending else [(name, False)])
            self.reset()
            return out
        if self.pending is not None and down:
            held, self.pending = self.pending, None
            if name.lower() in self.keys:
                self.mode = "ctrl"
                return [(CONTROL, True), (name, True)]
            self.mode = "win"
            return [(held, True), (name, True)]
        return [(name, down)]

    def flush(self) -> List[Event]:
        """A mouse button with Command held back: it is the Windows key after all."""
        if self.pending is None:
            return []
        held, self.pending, self.mode = self.pending, None, "win"
        return [(held, True)]

    def reset(self) -> None:
        self.pending = self.mode = self.command = None
