"""High-level async interface to a FlowToys bridge."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from types import TracebackType

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError

from . import _protocol
from .exceptions import NotABridgeError, PayloadTooLargeError, UnknownGroupError
from .models import MAX_COUNTER, PACKET_SIZE, Flags, Mode, Packet

log = logging.getLogger(__name__)

PacketListener = Callable[[Packet], None]


class Bridge:
    """Sends packets to FlowToys props through a bridge, and tracks what it hears.

    The caller owns the :class:`bleak.BleakClient` and is responsible for
    connecting and disconnecting it. Use the bridge as an async context manager
    so it listens for the packets the bridge reports::

        async with BleakClient(device) as client, Bridge(client) as bridge:
            await bridge.wait_for_group()
            await bridge.set_mode(Mode(page=1, index=2))

    Props only apply a packet whose counter is newer than the last one they
    saw, so the bridge must know each group's counter before it can control
    it. It learns them from the packets the bridge reports (press a prop's
    button 5 times quickly, or change its mode), or from :meth:`add_group`.

    Like the official app, every packet is written ``repeats`` times,
    ``repeat_interval`` seconds apart. Only the first write is awaited; a newer
    packet for the same group cancels the remaining repeats.
    """

    def __init__(
        self, client: BleakClient, *, repeats: int = 3, repeat_interval: float = 0.2
    ) -> None:
        if repeats < 1:
            raise ValueError(f"repeats must be at least 1, got {repeats}")
        self._client = client
        self._repeats = repeats
        self._repeat_interval = repeat_interval
        self._lock = asyncio.Lock()
        self._groups: dict[int, Packet] = {}
        self._listeners: list[PacketListener] = []
        self._repeat_tasks: dict[int, asyncio.Task[None]] = {}
        self._group_heard = asyncio.Event()

    @property
    def client(self) -> BleakClient:
        """The underlying BLE client."""
        return self._client

    @property
    def groups(self) -> dict[int, Packet]:
        """The latest packet heard or sent for each known group, by group id."""
        return dict(self._groups)

    async def __aenter__(self) -> Bridge:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start listening for the packets the bridge reports."""
        await self._client.start_notify(_protocol.EVENT_CHAR_UUID, self._on_event)

    async def stop(self) -> None:
        """Stop listening and cancel pending repeat writes."""
        for task in self._repeat_tasks.values():
            task.cancel()
        self._repeat_tasks.clear()
        if self._client.is_connected:
            with contextlib.suppress(BleakError):
                await self._client.stop_notify(_protocol.EVENT_CHAR_UUID)

    def add_listener(self, listener: PacketListener) -> Callable[[], None]:
        """Call ``listener`` with every packet the bridge reports.

        Returns a function that removes the listener again.
        """
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def add_group(self, group_id: int, counter: int = 0) -> None:
        """Make a group known without hearing from it first.

        ``counter`` must be at least the last counter the props saw, or they
        will ignore the next packets until the counter catches up.
        """
        current = self._groups.get(group_id)
        if current is None or current.counter < counter:
            self._groups[group_id] = Packet(group_id=group_id, counter=counter)
        self._group_heard.set()

    async def wait_for_group(self, timeout: float | None = None) -> None:
        """Wait until at least one group is known."""
        await asyncio.wait_for(self._group_heard.wait(), timeout)

    async def wake_up(self, group: int | None = None) -> None:
        """Turn on the props in ``group``, or in every known group if ``None``."""
        await self._update(group, lambda base: base.replace(flags=Flags.WAKE_UP))

    async def power_off(self, group: int | None = None) -> None:
        """Turn off the props in ``group``, or in every known group if ``None``."""
        await self._update(group, lambda base: base.replace(flags=Flags.POWER_OFF))

    async def set_mode(self, mode: Mode, group: int | None = None, *, reload: bool = True) -> None:
        """Switch the props in ``group``, or in every known group, to ``mode``.

        With ``reload`` (the default) the props restart the pattern, as the app
        does on every pattern change. Pass ``reload=False`` to change only the
        parameters of the running pattern, e.g. during a fade.

        For continuous updates (e.g. from a slider) limit the rate to about one
        call every 50 ms.
        """
        flags = Flags.FORCE_RELOAD if reload else Flags.NONE
        await self._update(
            group,
            lambda base: Packet.from_mode(mode, group_id=base.group_id, counter=0, flags=flags),
        )

    async def send(self, packet: Packet) -> None:
        """Send a packet exactly as given, including its counter."""
        async with self._lock:
            await self._transmit(packet)

    async def _update(self, group: int | None, change: Callable[[Packet], Packet]) -> None:
        async with self._lock:
            for base in self._targets(group):
                counter = (base.counter + 1) & MAX_COUNTER
                await self._transmit(change(base).replace(counter=counter))

    def _targets(self, group: int | None) -> list[Packet]:
        if group is None:
            if not self._groups:
                raise UnknownGroupError(
                    "no groups known yet; press a prop's button 5 times quickly (or change its "
                    "mode) so the bridge hears it, or use add_group()"
                )
            return list(self._groups.values())
        if group not in self._groups:
            raise UnknownGroupError(
                f"group 0x{group:04x} not heard yet; its packet counter is unknown, "
                "use add_group() if you know it"
            )
        return [self._groups[group]]

    async def _transmit(self, packet: Packet) -> None:
        char = self._command_characteristic()
        payload = packet.to_bytes()
        await self._client.write_gatt_char(char, payload, response=False)
        self._remember(packet)
        pending = self._repeat_tasks.pop(packet.group_id, None)
        if pending is not None:
            pending.cancel()
        if self._repeats > 1:
            self._repeat_tasks[packet.group_id] = asyncio.create_task(self._repeat(char, payload))

    async def _repeat(self, char: BleakGATTCharacteristic, payload: bytes) -> None:
        for _ in range(self._repeats - 1):
            await asyncio.sleep(self._repeat_interval)
            try:
                await self._client.write_gatt_char(char, payload, response=False)
            except BleakError as e:
                log.debug("repeat write failed: %s", e)
                return

    def _command_characteristic(self) -> BleakGATTCharacteristic:
        char = self._client.services.get_characteristic(_protocol.COMMAND_CHAR_UUID)
        if char is None:
            raise NotABridgeError("device has no FlowToys bridge command characteristic")
        if char.max_write_without_response_size < PACKET_SIZE:
            raise PayloadTooLargeError(
                f"packets are {PACKET_SIZE} bytes but this connection allows writes of at most "
                f"{char.max_write_without_response_size}; a larger MTU is needed"
            )
        return char

    def _remember(self, packet: Packet) -> None:
        current = self._groups.get(packet.group_id)
        if current is None or packet.counter >= current.counter:
            self._groups[packet.group_id] = packet
        self._group_heard.set()

    def _on_event(self, _: BleakGATTCharacteristic, data: bytearray) -> None:
        log.debug("notification %s", data.hex(" "))
        packet = _protocol.decode_event(bytes(data))
        if packet is None:
            return
        self._remember(packet)
        for listener in list(self._listeners):
            listener(packet)
