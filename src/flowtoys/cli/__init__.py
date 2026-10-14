"""Control FlowToys props interactively, or bridge DMX (sACN) to them.

Examples:
  flowtoys                                interactive prompt
  flowtoys -g 1:1f37 -g 8:2a01            ...and bridge sACN to two groups
  flowtoys -d 50                          ...and run a dancefloor at DMX 50
  flowtoys -g 1:1f37 -d 50 --no-prompt    bridge only, e.g. as a service
  flowtoys -g 1:1f37 --dry-run            no bridge needed: log the packets
  flowtoys --write-fixture .              save the QLC+ fixture definition

It connects to the bridge at ADDRESS, or to the nearest one found by scanning,
and gives a prompt for sending commands (type 'help'). With -g or -d it also
listens for DMX over E1.31 (sACN), e.g. from QLC+, and sends it to the props.
Each group is controlled through a fixture (save its QLC+ definition with
--write-fixture):

-g DMX_ADDRESS:GROUP binds the fixture at a DMX start address to a group
(hex id), like a Docker port binding; add :extended for a fixture in Extended
mode. Repeat it for more groups.

-d DMX_ADDRESS makes the fixture at that address a dancefloor: any other group
whose prop announces itself, by pressing its button 5 times quickly, joins it
and follows that fixture. Changing the mode on the prop leaves the dancefloor.

In QLC+, send the universe with the E1.31 output plugin as unicast to
127.0.0.1. Groups can only be controlled once the bridge has heard them (5
presses, or a mode change on the prop), unless --start-counter is given.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import threading
from pathlib import Path

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .. import __version__, sacn
from ..bridge import Bridge
from ..discovery import discover_bridges
from ..dmx import Router
from ..exceptions import FlowtoysError
from ..models import Packet
from ..qlc import FILE_NAME, fixture_definition
from . import console
from ._sacn import DryRunTarget, check_mapping, parse_binding, parse_dancefloor, serve

with contextlib.suppress(ImportError):
    import readline  # noqa: F401  (enables line editing and history for input())

log = logging.getLogger("flowtoys.cli")

RECONNECT_DELAY = 2.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowtoys",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        epilog=__doc__.split("\n\n", 1)[1] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("address", nargs="?", help="bridge BLE address (default: scan)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug output")
    parser.add_argument(
        "--write-fixture",
        type=Path,
        metavar="PATH",
        help=f"save the QLC+ fixture definition to PATH (a file, or a folder for {FILE_NAME}) "
        "and exit",
    )
    parser.add_argument(
        "--start-counter",
        type=int,
        metavar="N",
        help="control mapped groups straight away, starting from counter N (e.g. 1000000)",
    )
    parser.add_argument(
        "--no-prompt", action="store_true", help="don't show the interactive prompt"
    )

    dmx = parser.add_argument_group("DMX bridge (E1.31 / sACN)")
    dmx.add_argument(
        "-g",
        "--group",
        dest="bindings",
        type=parse_binding,
        action="append",
        default=[],
        metavar="DMX_ADDRESS:GROUP[:extended]",
        help="bind the fixture at a DMX start address to a group (hex id); repeat for more",
    )
    dmx.add_argument(
        "-d",
        "--dancefloor",
        type=parse_dancefloor,
        metavar="DMX_ADDRESS[:extended]",
        help="the fixture that groups announcing themselves (5 presses) join and follow",
    )
    dmx.add_argument("-u", "--universe", type=int, default=1, help="sACN universe (default 1)")
    dmx.add_argument(
        "--listen", default="127.0.0.1", help="address to receive unicast on (default 127.0.0.1)"
    )
    dmx.add_argument("--port", type=int, default=sacn.PORT, help="UDP port (default 5568)")
    dmx.add_argument(
        "--multicast", action="store_true", help="join the universe's multicast group instead"
    )
    dmx.add_argument(
        "--min-interval",
        type=float,
        default=0.05,
        metavar="SECONDS",
        help="minimum time between packets for a mapped group (default 0.05)",
    )
    dmx.add_argument(
        "--dancefloor-interval",
        type=float,
        default=0.25,
        metavar="SECONDS",
        help="minimum time between packets for a dancefloor group (default 0.25)",
    )
    dmx.add_argument(
        "--dry-run", action="store_true", help="log packets instead of connecting to a bridge"
    )
    return parser


def make_router(target: Bridge | DryRunTarget, options: argparse.Namespace) -> Router | None:
    if not options.bindings and options.dancefloor is None:
        return None
    return Router(
        target,
        dict(options.bindings),
        dancefloor=options.dancefloor,
        min_interval=options.min_interval,
        dancefloor_interval=options.dancefloor_interval,
    )


def log_mapping(options: argparse.Namespace) -> None:
    for group, fixture in options.bindings:
        log.info("group %04x follows %s", group, fixture)
    if options.dancefloor is not None:
        log.info("dancefloor at %s: press a prop's button 5 times to join", options.dancefloor)


async def find_device(address: str | None) -> BLEDevice | str:
    if address:
        return address
    log.info("scanning for bridges...")
    bridges = await discover_bridges()
    if not bridges:
        sys.exit("no Flowtoys Bridge found")
    log.info("found %s (%s)", bridges[0].name, bridges[0].address)
    return bridges[0]


async def connect(client: BleakClient, bridge: Bridge, disconnected: asyncio.Event) -> None:
    """Connect, retrying until it works."""
    while True:
        disconnected.clear()
        try:
            await client.connect()
            await bridge.start()
        except (BleakError, asyncio.TimeoutError, OSError) as e:
            log.warning("could not connect (%s); retrying in %.0f s", e, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)
            continue
        log.info("connected to bridge %s", client.address)
        return


async def keep_connected(
    client: BleakClient, bridge: Bridge, disconnected: asyncio.Event, router: Router | None
) -> None:
    while True:
        await disconnected.wait()
        log.warning("bridge disconnected; reconnecting")
        await connect(client, bridge, disconnected)
        if router is not None:
            router.resume()


class LineReader:
    """Reads prompt lines in a daemon thread, so exiting never waits on ``input()``."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        self._wanted = threading.Event()
        threading.Thread(target=self._read, name="flowtoys-prompt", daemon=True).start()

    def _read(self) -> None:
        while True:
            self._wanted.wait()
            self._wanted.clear()
            try:
                line: str | None = input("flowtoys> ")
            except EOFError:
                line = None
            self._loop.call_soon_threadsafe(self._lines.put_nowait, line)
            if line is None:
                return

    async def readline(self) -> str | None:
        """The next line, or ``None`` at end of input."""
        self._wanted.set()
        return await self._lines.get()


async def prompt(bridge: Bridge, router: Router | None) -> None:
    print("type 'help' for commands")
    reader = LineReader()
    while True:
        line = await reader.readline()
        if line is None:
            return
        if not line.strip():
            continue
        try:
            if not await console.run_command(bridge, router, line):
                return
        except (ValueError, KeyError, IndexError, TypeError, FlowtoysError, BleakError) as e:
            print(f"error: {e or type(e).__name__}")


async def run(options: argparse.Namespace, interactive: bool) -> None:
    device = await find_device(options.address)
    disconnected = asyncio.Event()
    client = BleakClient(device, disconnected_callback=lambda _: disconnected.set())
    bridge = Bridge(client)
    router = make_router(bridge, options)

    def on_heard(packet: Packet) -> None:
        log.info(
            "heard %s%s", console.describe(packet), console.describe_role(router, packet.group_id)
        )
        if router is not None:
            router.on_heard(packet)

    bridge.add_listener(on_heard)
    await connect(client, bridge, disconnected)
    if options.start_counter is not None:
        for group, _ in options.bindings:
            bridge.add_group(group, options.start_counter)
        log.warning(
            "took control from counter %d; other remotes (like the app) are ignored until "
            "they catch up",
            options.start_counter,
        )

    async with contextlib.AsyncExitStack() as stack:
        if router is not None:
            await stack.enter_async_context(serve(router, options))
        reconnecting = asyncio.create_task(keep_connected(client, bridge, disconnected, router))
        try:
            if interactive:
                await prompt(bridge, router)
            else:
                await asyncio.Event().wait()
        finally:
            reconnecting.cancel()
            await bridge.stop()
            await client.disconnect()


async def run_dry(options: argparse.Namespace) -> None:
    counters = {group: options.start_counter or 0 for group, _ in options.bindings}
    router = make_router(DryRunTarget(counters), options)
    assert router is not None
    async with serve(router, options):
        await asyncio.Event().wait()


def write_fixture(path: Path) -> None:
    if path.is_dir():
        path = path / FILE_NAME
    path.write_text(fixture_definition(), encoding="utf-8")
    print(f"wrote {path}")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    options = parser.parse_args(argv)
    if options.write_fixture is not None:
        try:
            write_fixture(options.write_fixture)
        except OSError as e:
            parser.exit(1, f"flowtoys: error: {e}\n")
        return
    try:
        check_mapping(options.bindings, options.dancefloor)
    except ValueError as e:
        parser.error(str(e))
    has_mapping = bool(options.bindings) or options.dancefloor is not None
    if options.dry_run and not has_mapping:
        parser.error("--dry-run needs -g or -d")
    interactive = not options.no_prompt and not options.dry_run and sys.stdin.isatty()
    if not interactive and not has_mapping:
        parser.error("nothing to do: give -g or -d, or run interactively")

    logging.basicConfig(
        level=logging.DEBUG if options.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("bleak").setLevel(logging.INFO)  # -v is for our own debug output
    log_mapping(options)
    try:
        asyncio.run(run_dry(options) if options.dry_run else run(options, interactive))
    except KeyboardInterrupt:
        pass
    except (BleakError, OSError) as e:
        parser.exit(1, f"flowtoys: error: {e}\n")
