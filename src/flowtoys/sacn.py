"""Receiving DMX over E1.31 (sACN), as sent by QLC+ and other lighting software."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from dataclasses import dataclass

PORT = 5568
MAX_UNIVERSE = 63999

_ACN_ID = b"ASC-E1.17\x00\x00\x00"
_VECTOR_ROOT_DATA = 0x00000004
_VECTOR_FRAMING_DATA = 0x00000002
_VECTOR_DMP_SET_PROPERTY = 0x02
_DMP_ADDRESS_TYPE = 0xA1
_OPTION_PREVIEW = 0x20
_OPTION_TERMINATED = 0x40
_HEADER_SIZE = 126  # up to and including the DMX start code


@dataclass(frozen=True)
class DmxFrame:
    """One E1.31 data packet: a universe's DMX slots from one source."""

    universe: int
    source_id: bytes
    """The sender's 16-byte CID, unique per source."""
    source_name: str
    priority: int
    sequence: int
    terminated: bool
    """The source has stopped sending this universe."""
    data: bytes
    """DMX slots 1 onwards, without the start code."""


def parse_frame(packet: bytes) -> DmxFrame | None:
    """Decode an E1.31 data packet, or return ``None`` if ``packet`` isn't one.

    Preview packets and packets with a non-zero (non-dimmer) start code are
    also ignored.
    """
    if len(packet) < _HEADER_SIZE or packet[4:16] != _ACN_ID:
        return None
    if int.from_bytes(packet[18:22], "big") != _VECTOR_ROOT_DATA:
        return None
    if int.from_bytes(packet[40:44], "big") != _VECTOR_FRAMING_DATA:
        return None
    if packet[117] != _VECTOR_DMP_SET_PROPERTY or packet[118] != _DMP_ADDRESS_TYPE:
        return None
    options = packet[112]
    count = int.from_bytes(packet[123:125], "big")  # start code + slots
    if options & _OPTION_PREVIEW or count < 1 or len(packet) < 125 + count:
        return None
    if packet[125] != 0:
        return None
    return DmxFrame(
        universe=int.from_bytes(packet[113:115], "big"),
        source_id=bytes(packet[22:38]),
        source_name=packet[44:108].split(b"\x00", 1)[0].decode("utf-8", "replace"),
        priority=packet[108],
        sequence=packet[111],
        terminated=bool(options & _OPTION_TERMINATED),
        data=bytes(packet[126 : 125 + count]),
    )


def is_out_of_order(last: int, sequence: int) -> bool:
    """Whether a packet with ``sequence`` is stale after one with ``last``.

    Uses the E1.31 rule: discard when the signed difference is in (-20, 0].
    """
    diff = ((sequence - last + 128) & 0xFF) - 128
    return -20 < diff <= 0


def multicast_address(universe: int) -> str:
    """The multicast group E1.31 uses for ``universe``."""
    return f"239.255.{universe >> 8}.{universe & 0xFF}"


FrameHandler = Callable[[DmxFrame], None]


class Receiver(asyncio.DatagramProtocol):
    """Passes the valid, in-order frames for one universe to ``on_frame``."""

    def __init__(self, universe: int, on_frame: FrameHandler) -> None:
        self._universe = universe
        self._on_frame = on_frame
        self._sequences: dict[bytes, int] = {}

    def datagram_received(self, data: bytes, addr: tuple[str | object, int]) -> None:
        frame = parse_frame(data)
        if frame is None or frame.universe != self._universe:
            return
        last = self._sequences.get(frame.source_id)
        if last is not None and is_out_of_order(last, frame.sequence):
            return
        self._sequences[frame.source_id] = frame.sequence
        self._on_frame(frame)


async def listen(
    universe: int,
    on_frame: FrameHandler,
    *,
    host: str = "127.0.0.1",
    port: int = PORT,
    multicast: bool = False,
) -> asyncio.DatagramTransport:
    """Receive ``universe`` and call ``on_frame`` for every frame.

    By default this listens for unicast on ``host``. With ``multicast`` it
    joins the universe's multicast group on every interface instead. Close the
    returned transport to stop.
    """
    if not 1 <= universe <= MAX_UNIVERSE:
        raise ValueError(f"universe must be between 1 and {MAX_UNIVERSE}, got {universe}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    if multicast:
        sock.bind(("", port))
        group = socket.inet_aton(multicast_address(universe)) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group)
    else:
        sock.bind((host, port))
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: Receiver(universe, on_frame), sock=sock
    )
    return transport
