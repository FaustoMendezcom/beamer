"""Shared monitors: physical monitors wired to two machines' inputs, switched between them over
DDC/CI (WIRE.md section 8, shared monitors). Platform-neutral: which monitors two machines share,
the Switch computer chord, and the shared picture while a machine's shared monitors show the other.

A monitor is known by its EDID identity, which every machine wired to it reads the same. Each
machine learns its own input code on a monitor by reading it while the monitor shows it, and
announces it; a monitor both machines announce is shared. Only the machine a monitor shows can
switch it, so the other asks with a `request`."""

import copy
from typing import Dict, Iterable, List, Optional, Set

from core import ways

SETTINGS_KEY = "shared_monitors"
# Below the lowest tile, so a parked machine touches nothing and no edge leads to it.
PARK_GAP = 40


class ChordWatch:
    """Recognises one recorded chord (ways.jump_chord's spelling) in a stream of key names, and
    swallows the chord's key down and its matching up. Fires `on_chord` once per press."""

    MODIFIERS = {"alt", "alt_r", "cmd", "cmd_r", "ctrl", "ctrl_r", "shift", "shift_r"}

    def __init__(self, on_chord):
        self.chord: Optional[str] = None
        self.recording = False
        self._on_chord = on_chord
        self._modifiers: Set[str] = set()
        self._pressed: Set[str] = set()
        self._swallowed: Set[str] = set()

    def feed(self, name: str, down: bool) -> bool:
        if not isinstance(name, str):
            return False
        if name in self.MODIFIERS:
            (self._modifiers.add if down else self._modifiers.discard)(name)
            return False
        if not down:
            self._pressed.discard(name)
            if name in self._swallowed:
                self._swallowed.discard(name)
                return True
            return False
        repeat = name in self._pressed
        self._pressed.add(name)
        if name in self._swallowed:
            return True
        if repeat or self.recording or not self.chord:
            return False
        if ways.jump_chord(self._modifiers, name) != self.chord:
            return False
        self._swallowed.add(name)
        self._on_chord()
        return True


def state(settings: dict) -> dict:
    """This machine's shared-monitor record: `host` and `host_since` (its claim to keep the shared
    monitors' place, and when it was made), `codes` {monitor id: own input code}, `peers`
    {peer id: [announced monitors]}, `key` (the chord or ""), `away` {monitor id: peer id}
    while a monitor shows that peer, and `home` (the tiles to restore)."""
    held = settings.get(SETTINGS_KEY)
    held = held if isinstance(held, dict) else {}
    codes = held.get("codes") if isinstance(held.get("codes"), dict) else {}
    peers = held.get("peers") if isinstance(held.get("peers"), dict) else {}
    away = held.get("away") if isinstance(held.get("away"), dict) else {}
    key = held.get("key") if isinstance(held.get("key"), str) and ways.valid_jump_key(held.get("key")) else ""
    home = held.get("home") if isinstance(held.get("home"), dict) else None
    since = held.get("host_since")
    return {"host": held.get("host") is True,
            "host_since": since if type(since) is int and since > 0 else 0,
            "codes": {k: v for k, v in codes.items() if isinstance(k, str) and type(v) is int and 1 <= v <= 255},
            "peers": {k: [m for m in v if isinstance(m, dict)] for k, v in peers.items()
                      if isinstance(k, str) and isinstance(v, list)},
            "away": {k: v for k, v in away.items() if isinstance(k, str) and isinstance(v, str)},
            "key": key, "home": home}


def shared_with(own_ids: Iterable[str], peers: Dict[str, List[dict]]) -> Dict[str, List[dict]]:
    """{peer id: [its announced monitors this machine also has]}, peers sharing nothing left out.
    A monitor announced by more than one peer goes to none of them: it cannot say whose it is."""
    own = set(own_ids)
    counts: Dict[str, int] = {}
    for listed in peers.values():
        for monitor in listed:
            counts[monitor.get("id")] = counts.get(monitor.get("id"), 0) + 1
    result = {}
    for peer, listed in peers.items():
        shared = [monitor for monitor in listed if monitor.get("id") in own and counts.get(monitor.get("id")) == 1]
        if shared:
            result[peer] = shared
    return result


def _bounds(frames):
    left = min(frame["x"] for frame in frames)
    top = min(frame["y"] for frame in frames)
    right = max(frame["x"] + frame["w"] for frame in frames)
    bottom = max(frame["y"] + frame["h"] for frame in frames)
    return {"x": int(round(left)), "y": int(round(top)),
            "w": max(1, int(round(right - left))), "h": max(1, int(round(bottom - top)))}


def away_layout(layout: dict, own_id: str, peer: str, away_keys: Iterable[str]) -> Optional[dict]:
    """The picture with `own_id`'s displays `away_keys` given to `peer`: this machine's tile cut
    down to the displays it still shows, and `peer`'s tile laid over the ones it now shows. None
    when this machine would keep no display, or has no tile. Reads the frames before the cut, so
    the result does not depend on display keys this machine no longer reports."""
    own = layout.get(own_id)
    away = set(away_keys)
    if not isinstance(own, dict) or not isinstance(own.get("displays"), dict) or not away:
        return None
    frames = ways._display_frames(own)
    kept = [frame for frame in frames if frame["display"] not in away]
    gone = [frame for frame in frames if frame["display"] in away]
    if not kept or not gone:
        return None
    result = copy.deepcopy(layout)
    tile = result[own_id]
    tile.update(_bounds(kept))
    tile["displays"] = {key: rect for key, rect in own["displays"].items() if key not in away}
    offsets = tile.get("display_offsets")
    if isinstance(offsets, dict):
        tile["display_offsets"] = {key: value for key, value in offsets.items() if key not in away}
    placed = dict(result.get(peer) or {})
    placed.update(_bounds(gone))
    result[peer] = placed
    return result


def home_layout(layout: dict, own_id: str, peer: str, home: dict, own_displays: dict) -> dict:
    """The picture with the shared displays back on `own_id`: its tile as `home` kept it, its
    displays as reported now, and `peer`'s tile left where it is unless it would overlap or touch
    this machine's (where it stood in for the shared displays), then parked below everything."""
    result = copy.deepcopy(layout)
    tile = result.setdefault(own_id, {})
    for key in ("x", "y", "w", "h"):
        if type(home.get(key)) is int:
            tile[key] = home[key]
    if own_displays:
        tile["displays"] = copy.deepcopy(own_displays)
    if isinstance(home.get("display_offsets"), dict):
        tile["display_offsets"] = copy.deepcopy(home["display_offsets"])
    if peer in result and _in_the_way(result, own_id, peer):
        result[peer] = park(result, peer)
    return result


def _in_the_way(layout: dict, own_id: str, peer: str) -> bool:
    """Whether `peer`'s tile overlaps or touches any of `own_id`'s displays."""
    tile = layout.get(peer)
    if not isinstance(tile, dict) or not all(type(tile.get(key)) is int for key in ("x", "y", "w", "h")):
        return False
    frames = ways._display_frames(layout[own_id])
    return any(ways._overlaps(tile, frame) or ways._rect_contact(frame, tile) is not None for frame in frames)


def park(layout: dict, peer: str) -> dict:
    """`peer`'s tile moved below every other tile with a gap, so nothing touches it."""
    tile = dict(layout[peer])
    others = [other for key, other in layout.items() if key != peer and isinstance(other, dict)]
    bottom = max((other["y"] + other["h"] for other in others), default=0)
    left = min((other["x"] for other in others), default=0)
    tile["x"], tile["y"] = left, bottom + PARK_GAP
    return tile
