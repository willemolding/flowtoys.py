"""DMX input for the ``flowtoys`` command: E1.31 (sACN) into a :class:`Router`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping

from .. import sacn
from ..dmx import Fixture, Router
from ..models import Flags, Mode, Packet

log = logging.getLogger("flowtoys.cli")

SOURCE_TIMEOUT = 2.5  # seconds without data before a source counts as lost (E1.31)


def _parse_fixture(address: str, mode: str, text: str, form: str) -> Fixture:
    if mode not in ("standard", "extended"):
        raise argparse.ArgumentTypeError(f"expected {form}, got {text!r}")
    try:
        return Fixture(int(address), extended=mode == "extended")
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r}: {e}") from e


def parse_binding(text: str) -> tuple[int, Fixture]:
    """Parse a ``DMX_ADDRESS:GROUP[:extended]`` binding into (group id, fixture)."""
    form = "DMX_ADDRESS:GROUP[:extended] (e.g. 1:1f37)"
    parts = text.split(":")
    if len(parts) == 2:
        parts.append("standard")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"expected {form}, got {text!r}")
    address, group, mode = parts
    fixture = _parse_fixture(address, mode, text, form)
    try:
        return int(group, 16), fixture
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r}: {e}") from e


def parse_dancefloor(text: str) -> Fixture:
    """Parse a ``DMX_ADDRESS[:extended]`` dancefloor fixture."""
    address, _, mode = text.partition(":")
    return _parse_fixture(address, mode or "standard", text, "DMX_ADDRESS[:extended] (e.g. 50)")


def check_mapping(bindings: list[tuple[int, Fixture]], dancefloor: Fixture | None) -> None:
    """Raise ``ValueError`` if a group is mapped twice or fixtures overlap."""
    groups = [group for group, _ in bindings]
    for group in groups:
        if groups.count(group) > 1:
            raise ValueError(f"group {group:04x} is mapped twice")
    fixtures = [(f"group {g:04x}", f) for g, f in bindings]
    if dancefloor is not None:
        fixtures.append(("the dancefloor", dancefloor))
    used: dict[int, str] = {}
    for name, fixture in fixtures:
        for channel in range(fixture.address, fixture.last_address + 1):
            if channel in used:
                raise ValueError(f"{used[channel]} and {name} overlap at channel {channel}")
            used[channel] = name


class DryRunTarget:
    """Stands in for a bridge: logs the packets it would send."""

    def __init__(self, groups: Mapping[int, int]) -> None:
        self._groups = {g: Packet(group_id=g, counter=c) for g, c in groups.items()}

    @property
    def groups(self) -> dict[int, Packet]:
        return self._groups

    async def set_mode(self, mode: Mode, group: int | None = None, *, reload: bool = True) -> None:
        assert group is not None
        previous = self._groups.get(group)
        packet = Packet.from_mode(
            mode,
            group_id=group,
            counter=previous.counter + 1 if previous else 1,
            flags=Flags.FORCE_RELOAD if reload else Flags.NONE,
        )
        self._groups[group] = packet
        log.info("would send %s", packet.to_bytes().hex(" "))

    async def send(self, packet: Packet) -> None:
        log.debug("would resend %s", packet.to_bytes().hex(" "))


class Watchdog:
    """Logs when sACN data starts and stops arriving."""

    def __init__(self) -> None:
        self._last_seen: float | None = None
        self._source = ""

    def seen(self, frame: sacn.DmxFrame) -> None:
        if frame.terminated:
            log.warning("%s stopped sending; holding the last state", frame.source_name)
            self._last_seen = None
            return
        if self._last_seen is None or frame.source_name != self._source:
            log.info("receiving sACN from %s", frame.source_name or "an unnamed source")
            self._source = frame.source_name
        self._last_seen = asyncio.get_running_loop().time()

    async def run(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            now = asyncio.get_running_loop().time()
            if self._last_seen is not None and now - self._last_seen > SOURCE_TIMEOUT:
                log.warning("no sACN data for %.1f s; holding the last state", SOURCE_TIMEOUT)
                self._last_seen = None


@contextlib.asynccontextmanager
async def serve(router: Router, options: argparse.Namespace) -> AsyncIterator[None]:
    """Receive sACN into ``router`` until the context exits."""
    watchdog = Watchdog()

    def on_frame(frame: sacn.DmxFrame) -> None:
        watchdog.seen(frame)
        if not frame.terminated:
            router.on_frame(frame.data)

    transport = await sacn.listen(
        options.universe,
        on_frame,
        host=options.listen,
        port=options.port,
        multicast=options.multicast,
    )
    where = (
        sacn.multicast_address(options.universe)
        if options.multicast
        else f"{options.listen}:{options.port}"
    )
    log.info("listening for sACN universe %d on %s", options.universe, where)
    tasks = [asyncio.create_task(watchdog.run()), asyncio.create_task(router.resend_forever())]
    try:
        yield
    finally:
        transport.close()
        router.close()
        for task in tasks:
            task.cancel()
