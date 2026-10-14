"""BLE framing of FlowToys bridge traffic.

The host writes raw 21-byte radio packets to the command characteristic, and
the bridge notifies the packets it hears on the event characteristic. See
``PROTOCOL.md`` in the repository for the full specification.
"""

from __future__ import annotations

from .models import PACKET_SIZE, Packet

SERVICE_UUID = "49550001-aad5-59bd-934c-023d807e01d5"
EVENT_CHAR_UUID = "49550002-aad5-59bd-934c-023d807e01d5"
COMMAND_CHAR_UUID = "49550003-aad5-59bd-934c-023d807e01d5"
DEVICE_NAME = "Flowtoys Bridge"


def decode_event(data: bytes) -> Packet | None:
    """Decode a notification, or return ``None`` if it isn't a radio packet.

    A radio packet notification is one byte (which varies between packets,
    possibly signal strength) followed by the 21-byte packet.
    """
    if len(data) == 1 + PACKET_SIZE:
        return Packet.from_bytes(data[1:])
    return None
