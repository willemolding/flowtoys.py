"""Driving props from DMX, with the channel layout of the QLC+ fixture.

Each group of props is patched like a DMX fixture
(``fixture_defs/Flowtoys-Bridge-Group.qxf``, see :mod:`flowtoys.qlc`):

=====  ==========  ==========================================================
Ch     Name        Meaning
=====  ==========  ==========================================================
1      Pattern     0 = no change, 1-255 = one page and mode each (``PATTERNS``)
2      Brightness  always applied, so 0 is dark
3      Hue         0 = the prop's own setting, otherwise the value
4      Saturation  0 = the prop's own setting, otherwise the value
5      Speed       0 = the prop's own setting, otherwise the value
6      Density     0 = the prop's own setting, otherwise the value
7      Retrigger   moving into 128-255 restarts the pattern
8-11   LFO 1-4     Extended mode only; all 0 = the prop's own LFOs
=====  ==========  ==========================================================

:class:`Router` turns DMX frames into :class:`~flowtoys.Bridge` commands. Each
group follows a fixture: its own, or a shared "dancefloor" fixture that groups
join by announcing themselves. It only sends when a fixture's channels
change, restarts the pattern only when it is changed or retriggered, and sends
at most one packet per group every ``min_interval`` seconds (the first one
immediately).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from bleak.exc import BleakError

from .exceptions import UnknownGroupError
from .models import Mode, Packet

log = logging.getLogger(__name__)

_PAGES: dict[int, list[str]] = {
    1: [
        "Play: Rainbow",
        "Play: Rainbow Drops",
        "Play: Bold",
        "Play: Lantern",
        "Play: Fire",
        "Play: Water",
        "Play: Earth",
        "Play: Air",
        "Play: Spirit",
        "Play: Pulse",
        "Play: Autoplay",
    ],
    2: [
        "Party: Candy",
        "Party: Petals",
        "Party: Love",
        "Party: Watermelon",
        "Party: Freedom",
        "Party: Microdots",
        "Party: Unicorn",
        "Party: Blue Blazer",
        "Party: Solar Flare",
        "Party: Strobe",
        "Party: Autoplay",
    ],
    3: [
        "Perform: Flamebow",
        "Perform: Alicorn",
        "Perform: Liquid Sugar",
        "Perform: Rainbow Dash",
        "Perform: Fireball",
        "Perform: Froth",
        "Perform: Jammin’",  # noqa: RUF001  (the official spelling)
        "Perform: Bolder",
        "Perform: Sunset",
        "Perform: Daybreak",
        "Perform: Autoplay",
    ],
    4: [f"Favorites: slot {i}" for i in range(1, 61)],
    5: [
        "Settings: Grouping",
        "Settings: Brightness",
        "Settings: White balance",
        "Settings: Kinetic response",
        "Settings: Auto-sleep/ wake",
        "Settings: Standby setting (pink indicator)",
        "Settings: placeholder (dim red)",
        "Settings: placeholder (dim red)",
        "Settings: placeholder (dim red)",
        "Settings: Reset to default",
        "Settings: Random autoplay",
    ],
    6: ["Shortcut to P1 M1"],
    7: ["Shortcut to P1 M1"],
    8: [f"Show page: slot {i}" for i in range(1, 50)],
    9: [f"Show page: slot {i}" for i in range(1, 50)],
    13: [
        *(f"Bonus {i}" for i in range(1, 35)),
        "Bonus 35 (Autoplay on Capsule 2.0)",
        *(f"Bonus {i}" for i in range(36, 51)),
        "Bonus: Autoplay (Vision)",
    ],
}


@dataclass(frozen=True)
class Pattern:
    """A page and mode that the Pattern channel can select."""

    page: int
    index: int
    name: str

    @property
    def label(self) -> str:
        """The name shown in QLC+, e.g. ``P3 M5 · Perform: Fireball``."""
        return f"P{self.page} M{self.index} · {self.name}"


PATTERNS: tuple[Pattern, ...] = tuple(
    Pattern(page, index, name)
    for page, names in _PAGES.items()
    for index, name in enumerate(names, start=1)
)
"""Every pattern, in DMX order: value ``v`` (1-255) selects ``PATTERNS[v - 1]``."""

assert len(PATTERNS) == 255

_BY_MODE = {(p.page, p.index): p for p in PATTERNS}

STANDARD_CHANNELS = ("pattern", "brightness", "hue", "saturation", "speed", "density", "retrigger")
EXTENDED_CHANNELS = (*STANDARD_CHANNELS, "lfo1", "lfo2", "lfo3", "lfo4")

GROUPING_SETTING = (5, 1)
"""(page, index) a prop reports when its button is pressed 5 times to announce its group."""


def pattern_for_value(value: int) -> Pattern | None:
    """The pattern a Pattern channel value selects, or ``None`` for 0 (no change)."""
    return PATTERNS[value - 1] if value else None


def describe_mode(mode: Mode) -> str:
    """A mode as its pattern label plus the parameters it overrides."""
    pattern = _BY_MODE.get((mode.page, mode.index))
    text = pattern.label if pattern else f"P{mode.page} M{mode.index}"
    params = [
        f"{name}={value}"
        for name, value in vars(mode).items()
        if name not in ("page", "index") and value is not None
    ]
    return " ".join([text, *params])


@dataclass(frozen=True)
class FixtureState:
    """The channel values of one fixture."""

    pattern: int = 0
    brightness: int = 0
    hue: int = 0
    saturation: int = 0
    speed: int = 0
    density: int = 0
    retrigger: int = 0
    lfo: tuple[int, int, int, int] = (0, 0, 0, 0)

    @classmethod
    def decode(cls, slots: bytes) -> FixtureState:
        """Read a fixture's channels, starting at its first. Missing ones count as 0."""
        s = slots[: len(EXTENDED_CHANNELS)].ljust(len(EXTENDED_CHANNELS), b"\x00")
        return cls(
            pattern=s[0],
            brightness=s[1],
            hue=s[2],
            saturation=s[3],
            speed=s[4],
            density=s[5],
            retrigger=s[6],
            lfo=(s[7], s[8], s[9], s[10]),
        )

    @property
    def is_blank(self) -> bool:
        return self == FixtureState()

    @property
    def retriggering(self) -> bool:
        return self.retrigger >= 128

    def to_mode(self, current: tuple[int, int] | None) -> Mode | None:
        """The mode these channels select.

        ``current`` is the (page, index) last selected, kept when the Pattern
        channel is 0 (no change). Returns ``None`` if that is needed but unknown.
        """
        pattern = pattern_for_value(self.pattern)
        page_index = (pattern.page, pattern.index) if pattern else current
        if page_index is None:
            return None
        return Mode(
            page=page_index[0],
            index=page_index[1],
            brightness=self.brightness,
            hue=self.hue or None,
            saturation=self.saturation or None,
            speed=self.speed or None,
            density=self.density or None,
            lfo=self.lfo if any(self.lfo) else None,
        )


@dataclass(frozen=True)
class Fixture:
    """Where a fixture is patched in the DMX universe."""

    address: int
    """1-based DMX start address."""
    extended: bool = False
    """Extended mode (with LFO channels) instead of Standard."""

    def __post_init__(self) -> None:
        last = 512 - self.channels + 1
        if not 1 <= self.address <= last:
            raise ValueError(f"address must be between 1 and {last}, got {self.address}")

    @property
    def channels(self) -> int:
        return len(EXTENDED_CHANNELS if self.extended else STANDARD_CHANNELS)

    @property
    def last_address(self) -> int:
        return self.address + self.channels - 1

    def slots(self, data: bytes) -> bytes:
        """This fixture's channels from a universe's DMX data."""
        return data[self.address - 1 : self.address - 1 + self.channels]

    def __str__(self) -> str:
        mode = "Extended" if self.extended else "Standard"
        return f"DMX {self.address}-{self.last_address} ({mode})"


class Target(Protocol):
    """Where the router sends commands: a :class:`~flowtoys.Bridge`, or a stand-in."""

    @property
    def groups(self) -> dict[int, Packet]: ...

    async def set_mode(
        self, mode: Mode, group: int | None = None, *, reload: bool = True
    ) -> None: ...

    async def send(self, packet: Packet) -> None: ...


@dataclass
class _Slot:
    group_id: int
    fixture: Fixture
    min_interval: float
    dancefloor: bool = False
    state: FixtureState | None = None
    sent: Mode | None = None
    packet: Packet | None = None
    pending: Mode | None = None
    reload: bool = False
    last_sent_at: float = -math.inf
    task: asyncio.Task[None] | None = None
    warned: set[str] = field(default_factory=set)

    def warn_once(self, key: str, message: str) -> None:
        """Log a warning about this group, once until :attr:`warned` is cleared."""
        if key not in self.warned:
            log.warning("group %04x: %s", self.group_id, message)
            self.warned.add(key)

    def requeue(self, mode: Mode, reload: bool) -> None:
        if self.pending is None:
            self.pending = mode
        self.reload |= reload


class Router:
    """Sends the DMX channels of fixtures to groups of props.

    Each group in ``groups`` follows its own fixture. With a ``dancefloor``
    fixture, any other group that announces itself (its prop's button pressed
    5 times, see ``PROTOCOL.md`` §6) joins the dancefloor and follows that
    fixture, until its mode is changed on the prop. Dancefloor groups are sent
    at most one packet every ``dancefloor_interval`` seconds, to share the
    radio between many groups.

    Feed it every DMX frame of the universe with :meth:`on_frame`, and every
    packet the bridge hears with :meth:`on_heard`. Run :meth:`resend_forever`
    in a task to resend each group's latest packet periodically, so props that
    missed it catch up.

    A change that can't be sent yet (the group's counter is unknown, or the
    write failed) is kept and sent when the group is heard or on
    :meth:`resume`.
    """

    def __init__(
        self,
        target: Target,
        groups: Mapping[int, Fixture] | None = None,
        *,
        dancefloor: Fixture | None = None,
        min_interval: float = 0.05,
        dancefloor_interval: float = 0.25,
        resend_interval: float = 1.0,
    ) -> None:
        self.target = target
        self.dancefloor = dancefloor
        self._dancefloor_interval = dancefloor_interval
        self._resend_interval = resend_interval
        self._slots = {
            group_id: _Slot(group_id, fixture, min_interval)
            for group_id, fixture in (groups or {}).items()
        }
        self._states: dict[Fixture, FixtureState] = {}
        self._last_pattern: dict[Fixture, int] = {}

    @property
    def mapped(self) -> list[int]:
        """The groups with their own fixture."""
        return [s.group_id for s in self._slots.values() if not s.dancefloor]

    @property
    def members(self) -> list[int]:
        """The groups currently on the dancefloor."""
        return [s.group_id for s in self._slots.values() if s.dancefloor]

    def role(self, group_id: int) -> str | None:
        """What drives ``group_id``: its fixture, ``"dancefloor"``, or ``None``."""
        slot = self._slots.get(group_id)
        if slot is None:
            return None
        return "dancefloor" if slot.dancefloor else str(slot.fixture)

    def on_frame(self, data: bytes) -> None:
        """Handle one frame of DMX data (slot 1 onwards)."""
        fixtures = {slot.fixture for slot in self._slots.values()}
        if self.dancefloor is not None:
            fixtures.add(self.dancefloor)
        for fixture in fixtures:
            state = FixtureState.decode(fixture.slots(data))
            previous = self._states.get(fixture)
            self._states[fixture] = state
            if state != previous and not (previous is None and state.is_blank):
                self._log_received(fixture, previous, state)
            if state.pattern:
                self._last_pattern[fixture] = state.pattern
        for slot in list(self._slots.values()):
            state = self._states[slot.fixture]
            previous, slot.state = slot.state, state
            if state != previous and not (previous is None and state.is_blank):
                self._apply(slot, previous, state)

    def on_heard(self, packet: Packet) -> None:
        """Handle a packet the bridge heard.

        Once a group is heard, whatever its fixture says is sent to it. A group
        announcing itself joins the dancefloor (if there is one) or, if already
        controlled, is sent its fixture's state again, as the announcement left
        its props on the Grouping setting.
        """
        group_id = packet.group_id
        announced = (packet.page, packet.index) == GROUPING_SETTING
        slot = self._slots.get(group_id)
        if slot is None:
            if announced and self.dancefloor is not None:
                self.join(group_id)
            return
        if slot.dancefloor and not announced:
            self.leave(group_id, "its mode was changed on the prop")
        elif slot.pending is not None:
            self._schedule(slot)
        elif announced or slot.sent is None:
            self._reapply(slot)

    def join(self, group_id: int) -> None:
        """Put ``group_id`` on the dancefloor, as if it had announced itself."""
        if self.dancefloor is None:
            raise ValueError("there is no dancefloor fixture")
        slot = self._slots.get(group_id)
        if slot is not None and not slot.dancefloor:
            raise ValueError(f"group {group_id:04x} already follows {slot.fixture}")
        if slot is None:
            slot = _Slot(group_id, self.dancefloor, self._dancefloor_interval, dancefloor=True)
            self._slots[group_id] = slot
            log.info("group %04x joined the dancefloor", group_id)
        self._reapply(slot)

    def leave(self, group_id: int, reason: str = "") -> None:
        """Take ``group_id`` off the dancefloor; the props keep what they show."""
        slot = self._slots.get(group_id)
        if slot is None or not slot.dancefloor:
            raise ValueError(f"group {group_id:04x} is not on the dancefloor")
        del self._slots[group_id]
        if slot.task is not None:
            slot.task.cancel()
        log.info(
            "group %04x left the dancefloor%s; press its button 5 times to rejoin",
            group_id,
            f" ({reason})" if reason else "",
        )

    def resume(self) -> None:
        """Send every change that is still waiting, e.g. after reconnecting."""
        for slot in self._slots.values():
            if slot.pending is not None:
                self._schedule(slot)

    async def resend_forever(self) -> None:
        """Every ``resend_interval``, resend each group's latest packet unchanged."""
        while True:
            await asyncio.sleep(self._resend_interval)
            now = asyncio.get_running_loop().time()
            for slot in list(self._slots.values()):
                if slot.packet is None or slot.pending is not None:
                    continue
                if now - slot.last_sent_at < self._resend_interval:
                    continue
                if self.target.groups.get(slot.group_id) != slot.packet:
                    continue  # the props have moved on since
                try:
                    await self.target.send(slot.packet)
                except BleakError as e:
                    log.debug("group %04x: resend failed: %s", slot.group_id, e)

    def close(self) -> None:
        """Cancel pending sends."""
        for slot in self._slots.values():
            if slot.task is not None:
                slot.task.cancel()

    def _log_received(
        self, fixture: Fixture, previous: FixtureState | None, state: FixtureState
    ) -> None:
        if fixture == self.dancefloor:
            name = "dancefloor"
        else:
            name = ", ".join(
                f"group {s.group_id:04x}" for s in self._slots.values() if s.fixture == fixture
            )
        if previous is None or state.pattern != previous.pattern:
            pattern = pattern_for_value(state.pattern)
            log.info(
                "received %s: Pattern %d = %s",
                name,
                state.pattern,
                pattern.label if pattern else "No change",
            )
        if state.retriggering and not (previous is not None and previous.retriggering):
            log.info("received %s: Retrigger", name)

    def _reapply(self, slot: _Slot) -> None:
        """Send the slot's fixture state again, restarting the pattern."""
        state = self._states.get(slot.fixture)
        slot.state = state
        if state is not None and not state.is_blank:
            self._apply(slot, None, state, force=True)

    def _apply(
        self,
        slot: _Slot,
        previous: FixtureState | None,
        state: FixtureState,
        *,
        force: bool = False,
    ) -> None:
        pattern_changed = state.pattern != 0 and (
            previous is None or state.pattern != previous.pattern
        )
        retriggered = state.retriggering and not (previous is not None and previous.retriggering)
        reload = force or pattern_changed or retriggered
        latest = slot.pending if slot.pending is not None else slot.sent
        if latest is not None:
            current: tuple[int, int] | None = (latest.page, latest.index)
        else:
            pattern = pattern_for_value(self._last_pattern.get(slot.fixture, 0))
            current = (pattern.page, pattern.index) if pattern else None
        mode = state.to_mode(current)
        if mode is None:
            slot.warn_once(
                "pattern",
                "Pattern is 'No change' but no pattern has been selected yet; select one first",
            )
            return
        if not reload and mode == latest:
            return
        slot.pending = mode
        slot.reload |= reload
        self._schedule(slot)

    def _schedule(self, slot: _Slot) -> None:
        if slot.task is not None and not slot.task.done():
            return
        loop = asyncio.get_running_loop()
        delay = max(0.0, slot.last_sent_at + slot.min_interval - loop.time())
        slot.task = loop.create_task(self._flush(slot, delay))

    async def _flush(self, slot: _Slot, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        mode, reload = slot.pending, slot.reload
        if mode is None:
            return
        slot.pending, slot.reload = None, False
        slot.last_sent_at = asyncio.get_running_loop().time()
        try:
            await self.target.set_mode(mode, slot.group_id, reload=reload)
        except UnknownGroupError:
            slot.requeue(mode, reload)
            slot.warn_once(
                "counter",
                f"not heard yet, so its counter is unknown; holding {describe_mode(mode)} "
                "until it is (press a prop's button 5 times quickly, or set a start counter)",
            )
            return
        except BleakError as e:
            slot.requeue(mode, reload)
            log.warning("group %04x: send failed, will retry: %s", slot.group_id, e)
            return
        finally:
            slot.task = None
        slot.warned.clear()
        slot.sent = mode
        slot.packet = self.target.groups.get(slot.group_id)
        log.info(
            "sent group %04x: %s%s",
            slot.group_id,
            describe_mode(mode),
            " (restart)" if reload else "",
        )
        if slot.pending is not None and self._slots.get(slot.group_id) is slot:
            self._schedule(slot)
