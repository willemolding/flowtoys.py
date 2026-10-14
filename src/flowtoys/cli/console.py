"""Interactive commands for the ``flowtoys`` prompt."""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

from .._protocol import COMMAND_CHAR_UUID
from ..bridge import Bridge
from ..dmx import GROUPING_SETTING, Router, describe_mode
from ..models import Mode, Packet

HELP = """\
GROUP is a radio group id in hex (e.g. 1f37). Without GROUP, commands go to
every known group. A group becomes known when the bridge hears it: press a
prop's button 5 times quickly (it announces its group), or change its mode.

  groups                        list known groups and what controls them
  group GROUP [COUNTER]         add a group by hand (counter defaults to 0)
  wake [GROUP]                  wake up props
  off [GROUP]                   power off props
  mode PAGE INDEX [GROUP] [hue=N] [sat=N] [bri=N] [speed=N] [density=N] [lfo=A,B,C,D]
                                set a mode (page/index are 1-based, values 0-255)
  status                        show the DMX mapping and dancefloor
  join GROUP                    put a group on the dancefloor
  leave GROUP                   take a group off the dancefloor
  raw [@CHAR] TEXT              write TEXT as-is (protocol debugging)
  rawhex [@CHAR] HEX            write bytes given as hex, e.g. rawhex @0004 01ff
                                (@CHAR is a characteristic UUID or its unique
                                suffix; default is the command characteristic)
  chars                         list the bridge's characteristics
  help                          show this help
  quit                          disconnect and exit

Commands sent while DMX controls a group last until its fixture next changes.
"""

MODE_KEYS = {
    "hue": "hue",
    "sat": "saturation",
    "bri": "brightness",
    "speed": "speed",
    "density": "density",
}


def parse_group(text: str) -> int:
    return int(text, 16)


def parse_mode(args: list[str]) -> tuple[Mode, int | None]:
    if len(args) < 2:
        raise ValueError("usage: mode PAGE INDEX [GROUP] [key=value ...]")
    group: int | None = None
    params: dict[str, int] = {}
    lfo: tuple[int, int, int, int] | None = None
    for arg in args[2:]:
        if "=" not in arg:
            group = parse_group(arg)
            continue
        key, _, value = arg.partition("=")
        if key == "lfo":
            values = tuple(int(v) for v in value.split(","))
            if len(values) != 4:
                raise ValueError("lfo needs 4 comma-separated values")
            lfo = (values[0], values[1], values[2], values[3])
        elif key in MODE_KEYS:
            params[MODE_KEYS[key]] = int(value)
        else:
            raise ValueError(f"unknown mode parameter {key!r}")
    mode = Mode(page=int(args[0]), index=int(args[1]), lfo=lfo, **params)
    return mode, group


def find_characteristic(client: BleakClient, ref: str) -> BleakGATTCharacteristic:
    """Look up a characteristic by full UUID or a unique suffix of its first part."""
    ref = ref.lower()
    matches = [
        char
        for service in client.services
        for char in service.characteristics
        if char.uuid.lower() == ref or char.uuid.lower().split("-")[0].endswith(ref)
    ]
    if len(matches) != 1:
        raise ValueError(f"{len(matches)} characteristics match {ref!r}, see 'chars'")
    return matches[0]


def parse_raw(client: BleakClient, line: str, cmd: str) -> tuple[BleakGATTCharacteristic, bytes]:
    """Parse ``raw``/``rawhex`` arguments into a target characteristic and payload."""
    parts = line.split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    target = COMMAND_CHAR_UUID
    if rest.startswith("@"):
        target, _, rest = rest[1:].partition(" ")
    char = find_characteristic(client, target)
    payload = bytes.fromhex(rest) if cmd == "rawhex" else rest.encode()
    if not payload:
        raise ValueError(f"usage: {cmd} [@CHAR] {'HEX' if cmd == 'rawhex' else 'TEXT'}")
    return char, payload


def describe(packet: Packet) -> str:
    """A packet as its group, counter and mode, e.g. for the log."""
    flags = f"  flags 0x{packet.flags:02x}" if packet.flags else ""
    if (packet.page, packet.index) == GROUPING_SETTING:
        what = "announcing itself"
    else:
        what = describe_mode(packet.mode)
    return f"group {packet.group_id:04x}  counter {packet.counter}  {what}{flags}"


def describe_role(router: Router | None, group_id: int) -> str:
    """What controls a group, as a suffix for :func:`describe`."""
    if router is None:
        return ""
    return f"  [{router.role(group_id) or 'not mapped'}]"


def print_characteristics(client: BleakClient) -> None:
    for service in client.services:
        print(f"service {service.uuid}")
        for char in service.characteristics:
            print(f"  {char.uuid}  {', '.join(char.properties)}")


def print_groups(bridge: Bridge, router: Router | None) -> None:
    if not bridge.groups:
        print("no groups known yet")
    for packet in bridge.groups.values():
        print(describe(packet) + describe_role(router, packet.group_id))


def print_status(router: Router | None) -> None:
    if router is None:
        print("no DMX mapping; start with -g or -d to act as a bridge")
        return
    for group_id in sorted(set(router.mapped)):
        print(f"group {group_id:04x}  {router.role(group_id)}")
    if router.dancefloor is None:
        print("no dancefloor")
        return
    members = ", ".join(f"{g:04x}" for g in router.members) or "nobody yet"
    print(f"dancefloor  {router.dancefloor}: {members}")


def require_router(router: Router | None) -> Router:
    if router is None:
        raise ValueError("no DMX mapping; start with -d to use a dancefloor")
    return router


async def run_command(bridge: Bridge, router: Router | None, line: str) -> bool:
    """Run one command line. Returns False when the user wants to quit."""
    cmd, *args = shlex.split(line)
    takes_group = ("wake", "off", "group", "join", "leave")
    group = parse_group(args[0]) if args and cmd in takes_group else None

    async def send_raw() -> None:
        char, payload = parse_raw(bridge.client, line, cmd)
        response = "write-without-response" not in char.properties
        await bridge.client.write_gatt_char(char, payload, response=response)

    actions: dict[str, Callable[[], Awaitable[None]]] = {
        "wake": lambda: bridge.wake_up(group),
        "off": lambda: bridge.power_off(group),
        "mode": lambda: bridge.set_mode(*parse_mode(args)),
        "raw": send_raw,
        "rawhex": send_raw,
    }

    if cmd in ("quit", "exit"):
        return False
    if cmd == "help":
        print(HELP)
    elif cmd == "chars":
        print_characteristics(bridge.client)
    elif cmd == "groups":
        print_groups(bridge, router)
    elif cmd == "status":
        print_status(router)
    elif cmd in ("group", "join", "leave"):
        if group is None:
            raise ValueError(f"usage: {cmd} GROUP" + (" [COUNTER]" if cmd == "group" else ""))
        if cmd == "group":
            bridge.add_group(group, int(args[1]) if len(args) > 1 else 0)
            print_groups(bridge, router)
        elif cmd == "join":
            require_router(router).join(group)
        else:
            require_router(router).leave(group)
    elif cmd in actions:
        await actions[cmd]()
        print("sent")
    else:
        print(f"unknown command {cmd!r}, type 'help'")
    return True
