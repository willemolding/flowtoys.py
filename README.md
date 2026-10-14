# flowtoys

Control [FlowToys](https://flowtoys.com) props through a
**Flowtoys Bridge** over Bluetooth LE.

Also bridge DMX over sACN (E1.31) messages from light controller software such as QLC+ allowing groups of props to be controlled as regular light fixtures

> [!IMPORTANT]
> This project is fully vibe coded for fun. Consider it a means to an end rather than a good piece of software.
> The code within is not a reflection of the coding abilities and standards of the author

## Installation

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), a
   tool for installing Python apps. It works on macOS and Windows, and installs
   Python for you if needed.

2. Install `flowtoys`:

   ```sh
   uv tool install git+https://github.com/willemolding/flowtoys.py
   ```

3. Open a new terminal window and check it works:

   ```sh
   flowtoys --version
   ```

Installing from GitHub needs [git](https://git-scm.com/downloads). The first
time `flowtoys` connects to a bridge, macOS asks to allow your terminal app to
use Bluetooth: allow it.

To update, run `uv tool upgrade flowtoys`. To remove it, run
`uv tool uninstall flowtoys`.

To use the Python library in your own project instead:

```sh
uv add git+https://github.com/willemolding/flowtoys.py
pip install git+https://github.com/willemolding/flowtoys.py    # or with pip
```

## The `flowtoys` command

This connects to the nearest bridge, or to the one whose address you give, and depending on the args runs in one of two modes

* **Interactive Mode** for controlling props by hand. Type `help` for the
  commands: `groups`, `mode 3 5 1f37 bri=200`, `wake`, `off`, and raw writes
  for protocol debugging.
* **A DMX bridge.** With `-g` or `-d`, it receives DMX over E1.31 (sACN), for
  example from QLC+, and sends it to the props, so lighting software can
  program them like fixtures.

The DMX bridge supports a number of modes. 

- **Dancefloor Mode** allows props groups to be dynamically added (by pressing button 5 times) and will map all groups to a single DMX start address. 
- **Mapped Mode** allows mapping specific prop groups (given by 4 hex chars) to specific start address (e.g. `1:1f37`). This is for more complex choreography where multiple groups might need to receive different commands

```sh
flowtoys                                # interactive prompt only
flowtoys -d 1                           # dancefloor mode (all groups) map to DMX 1
flowtoys -g 1:1f37 -g 8:2a01            # map two specific groups to DMX 1 and 8
```

A group can only be controlled once the bridge has heard it, which tells it
the group's counter (see [How it works](#how-it-works)). Press a prop's button
**5 times quickly** to make it announce its group, or change its mode on the
prop.

### Mapping groups to DMX

Each group of props is controlled through a DMX fixture,
[fixture_defs/Flowtoys-Bridge-Group.qxf](fixture_defs/Flowtoys-Bridge-Group.qxf):

| Ch | Name | Meaning |
|---|---|---|
| 1 | Pattern | 0 = no change, 1-255 = one page and mode each, named in QLC+ |
| 2 | Brightness | always applied, so 0 is dark |
| 3-6 | Hue, Saturation, Speed, Density | 0 = the prop's own setting, otherwise the value |
| 7 | Retrigger | moving into 128-255 restarts the pattern |
| 8-11 | LFO 1-4 | Extended mode only |

At the prompt, `status` shows the mapping and who is on the dancefloor, and
`join GROUP` and `leave GROUP` change it by hand.

### Setting up QLC+

1. Copy the fixture definition to QLC+'s fixtures folder
   (`~/Library/Application Support/QLC+/Fixtures/` on macOS) and restart QLC+.
   Patch one **Flowtoys Bridge Group** per `-g` group, and one for the
   dancefloor.
2. Set the universe's output to the **E1.31** plugin, unicast to `127.0.0.1`,
   E1.31 universe 1.
3. Start `flowtoys` with the same addresses.

The bridge sends a group's packet only when its fixture changes, and restarts
the pattern only when the Pattern channel changes or on Retrigger. It resends
the latest packet every second for props that missed it, holds the last state
if sACN stops, and reconnects to the bridge if the connection drops. See
`flowtoys --help` for all options.

## How it works

The bridge relays FlowToys radio packets in both directions. Every packet
carries the full state of one **group** of props: a 16-bit group id, the
mode, and a **counter**. Props ignore any packet whose counter isn't
newer than the last one they saw.

So before `Bridge` can control a group, it needs that group's current
counter. It learns it from the packets the bridge reports: press a prop's button
5 times quickly (it announces its group), or change its mode on the prop.
Until then, commands for that group
raise `UnknownGroupError`. If you know a group's id and counter, register it
with `add_group()`.

## Protocol

The bridge's BLE protocol is not officially documented.
[PROTOCOL.md](PROTOCOL.md) is a language-independent specification,
reverse-engineered from a capture of the official app. It also lists what is
still unknown.
