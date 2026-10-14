"""Value types for FlowToys radio packets."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import IntFlag

PACKET_SIZE = 21
MAX_GROUP_ID = 0xFFFF
MAX_COUNTER = 0xFFFFFFFF


class Overrides(IntFlag):
    """Which mode parameters in a packet the props should use (offset 15)."""

    NONE = 0
    LFO = 0x01
    HUE = 0x02
    SATURATION = 0x04
    BRIGHTNESS = 0x08
    SPEED = 0x10
    DENSITY = 0x20


class Flags(IntFlag):
    """Command flags of a packet (offset 20)."""

    NONE = 0
    ADJUST = 0x01
    WAKE_UP = 0x02
    POWER_OFF = 0x04
    FORCE_RELOAD = 0x08
    SAVE = 0x10
    DELETE = 0x20
    ALTERNATE = 0x40


def _check_range(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")


def _check_byte(name: str, value: int) -> None:
    _check_range(name, value, 0, 255)


def _check_lfo(lfo: tuple[int, ...]) -> None:
    if len(lfo) != 4:
        raise ValueError(f"lfo must have exactly 4 values, got {len(lfo)}")
    for i, value in enumerate(lfo, start=1):
        _check_byte(f"lfo{i}", value)


@dataclass(frozen=True, kw_only=True)
class Mode:
    """A mode (its page and index within the page) plus optional parameter overrides.

    ``page`` and ``index`` are 1-based, matching the numbering on the props.
    Each parameter is a value from 0 to 255; leaving it as ``None`` keeps the
    prop's own setting for that parameter.
    """

    page: int
    index: int
    hue: int | None = None
    saturation: int | None = None
    brightness: int | None = None
    speed: int | None = None
    density: int | None = None
    lfo: tuple[int, int, int, int] | None = None

    def __post_init__(self) -> None:
        _check_range("page", self.page, 1, 256)
        _check_range("index", self.index, 1, 256)
        for name in ("hue", "saturation", "brightness", "speed", "density"):
            value = getattr(self, name)
            if value is not None:
                _check_byte(name, value)
        if self.lfo is not None:
            _check_lfo(self.lfo)


_PARAMS = (
    ("hue", Overrides.HUE),
    ("saturation", Overrides.SATURATION),
    ("brightness", Overrides.BRIGHTNESS),
    ("speed", Overrides.SPEED),
    ("density", Overrides.DENSITY),
)


@dataclass(frozen=True, kw_only=True)
class Packet:
    """One FlowToys radio packet: the full state of a group of props.

    The bridge transmits packets written to it as-is, and reports the packets
    it hears from props and other remotes. ``page`` and ``index`` (the mode
    within the page) are 1-based; every other field is the raw value on the
    wire. See ``PROTOCOL.md`` §2.
    """

    group_id: int
    counter: int
    page: int = 1
    index: int = 1
    lfo: tuple[int, int, int, int] = (0, 0, 0, 0)
    hue: int = 0
    saturation: int = 0
    brightness: int = 0
    speed: int = 0
    density: int = 0
    overrides: Overrides = Overrides.NONE
    reserved: int = 0
    flags: Flags = Flags.NONE

    def __post_init__(self) -> None:
        _check_range("group_id", self.group_id, 0, MAX_GROUP_ID)
        _check_range("counter", self.counter, 0, MAX_COUNTER)
        _check_range("page", self.page, 1, 256)
        _check_range("index", self.index, 1, 256)
        _check_lfo(self.lfo)
        for name, _ in _PARAMS:
            _check_byte(name, getattr(self, name))
        _check_byte("overrides", self.overrides)
        _check_range("reserved", self.reserved, 0, 0xFFFF)
        _check_byte("flags", self.flags)

    @classmethod
    def from_mode(
        cls, mode: Mode, *, group_id: int, counter: int, flags: Flags = Flags.FORCE_RELOAD
    ) -> Packet:
        """Build the packet that switches ``group_id`` to ``mode``."""
        overrides = Overrides.NONE if mode.lfo is None else Overrides.LFO
        values: dict[str, int] = {}
        for name, bit in _PARAMS:
            value = getattr(mode, name)
            if value is not None:
                overrides |= bit
            values[name] = value or 0
        return cls(
            group_id=group_id,
            counter=counter,
            page=mode.page,
            index=mode.index,
            lfo=mode.lfo or (0, 0, 0, 0),
            overrides=overrides,
            flags=flags,
            **values,
        )

    @property
    def mode(self) -> Mode:
        """The mode this packet selects, with only its active overrides."""
        values = {
            name: getattr(self, name) if bit in self.overrides else None for name, bit in _PARAMS
        }
        lfo = self.lfo if Overrides.LFO in self.overrides else None
        return Mode(page=self.page, index=self.index, lfo=lfo, **values)

    def replace(self, **changes: object) -> Packet:
        """Return a copy with some fields changed."""
        return dataclasses.replace(self, **changes)  # type: ignore[arg-type]

    def to_bytes(self) -> bytes:
        """Encode the packet into its 21-byte wire form."""
        return b"".join(
            [
                self.group_id.to_bytes(2, "big"),
                self.counter.to_bytes(4, "little"),
                bytes(self.lfo),
                bytes([self.hue, self.saturation, self.brightness, self.speed, self.density]),
                bytes([self.overrides]),
                self.reserved.to_bytes(2, "little"),
                bytes([self.page - 1, self.index - 1, self.flags]),
            ]
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> Packet:
        """Decode a 21-byte wire packet."""
        if len(data) != PACKET_SIZE:
            raise ValueError(f"a packet is {PACKET_SIZE} bytes, got {len(data)}")
        return cls(
            group_id=int.from_bytes(data[0:2], "big"),
            counter=int.from_bytes(data[2:6], "little"),
            lfo=(data[6], data[7], data[8], data[9]),
            hue=data[10],
            saturation=data[11],
            brightness=data[12],
            speed=data[13],
            density=data[14],
            overrides=Overrides(data[15]),
            reserved=int.from_bytes(data[16:18], "little"),
            page=data[18] + 1,
            index=data[19] + 1,
            flags=Flags(data[20]),
        )
