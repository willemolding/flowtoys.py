"""Finding FlowToys bridges over Bluetooth LE."""

from __future__ import annotations

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from ._protocol import DEVICE_NAME, SERVICE_UUID


async def discover_bridges(timeout: float = 5.0) -> list[BLEDevice]:
    """Scan for nearby bridges and return them, closest first.

    Bridges are recognised by their advertised service UUID, or by their name
    ``Flowtoys Bridge``. Pass a returned device to :class:`bleak.BleakClient`
    to connect.
    """
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    bridges = [
        (device, adv.rssi)
        for device, adv in found.values()
        if SERVICE_UUID in (u.lower() for u in adv.service_uuids)
        or (adv.local_name or device.name) == DEVICE_NAME
    ]
    bridges.sort(key=lambda pair: pair[1], reverse=True)
    return [device for device, _ in bridges]
