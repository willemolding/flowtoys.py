# Flowtoys Bridge protocol

The Bluetooth LE protocol of the **Flowtoys Bridge**, which relays FlowToys
radio packets between a host and props. It is not officially documented; this
description was reverse-engineered and confirmed on hardware, except where
marked *(unverified)*.

```
host ──BLE write──▶ bridge ──radio──▶ props
host ◀─BLE notify── bridge ◀─radio─── props and other remotes
```

The bridge does not interpret packets. The host builds every packet itself.

## 1. Transport

| | |
|---|---|
| Advertised name | `Flowtoys Bridge` |
| Service | `49550001-AAD5-59BD-934C-023D807E01D5` |
| Pairing | none |
| Connections | one central at a time |
| ATT MTU | at least 24 (a packet is one 21-byte write); the bridge offers 64 |

Characteristics (all `4955000X-AAD5-59BD-934C-023D807E01D5`):

| UUID | Properties | Use |
|---|---|---|
| `49550003-…` | Write, Write Without Response | commands: host → bridge (§3) |
| `49550002-…` | Notify | events: bridge → host (§4) |
| `49550004-…` | Write, Write Without Response | unknown |
| `49550005-…` | Write, Write Without Response | unknown |

The host enables notifications on `49550002-…` after connecting.

## 2. Radio packet

21 bytes. Multi-byte fields are little-endian, except `group_id`.

| Offset | Size | Field | |
|---|---|---|---|
| 0 | 2 | `group_id` | **big-endian** (§5) |
| 2 | 4 | `counter` | §5 |
| 6 | 4 | `lfo[0..3]` | LFO values 1–4 |
| 10 | 1 | `hue` | hue offset |
| 11 | 1 | `saturation` | |
| 12 | 1 | `brightness` | |
| 13 | 1 | `speed` | |
| 14 | 1 | `density` | |
| 15 | 1 | `overrides` | below |
| 16 | 2 | reserved | `0` |
| 18 | 1 | `page` | 0-based |
| 19 | 1 | `mode` | 0-based |
| 20 | 1 | `flags` | below |

**Page and mode** are numbered from 1 on the props and from 0 on the wire:
page 1 mode 2 is sent as `page=0`, `mode=1`.

**Overrides.** A prop applies a parameter only if its bit is set; otherwise it
keeps its own setting and the value is ignored (send `0`).

| Mask | Fields |
|---|---|
| `0x01` | `lfo[0..3]` |
| `0x02` | `hue` |
| `0x04` | `saturation` |
| `0x08` | `brightness` |
| `0x10` | `speed` |
| `0x20` | `density` |

**Flags.**

| Mask | Name | Meaning |
|---|---|---|
| `0x01` | `adjust_active` | *(unverified)* |
| `0x02` | `wakeup` | turn the props on *(unverified)* |
| `0x04` | `poweroff` | turn the props off *(unverified)* |
| `0x08` | `force_reload` | (re)start the pattern; set on a pattern change |
| `0x10` | `save` | *(unverified)* |
| `0x20` | `delete` | *(unverified)* |
| `0x40` | `alternate` | *(unverified)* |

## 3. Commands

Each write to `49550003-…` is one radio packet, with no framing. The bridge
transmits it to the props of its `group_id`.

- Use Write Without Response.
- Send each packet 3 times, about 200 ms apart, with the same counter, to
  cover lost radio packets. Props ignore the duplicates.
- Send at most one new packet per group every 50 ms. A newer packet replaces
  the pending repeats of an older one.
- To wake up or power off a group, send its current packet with `wakeup` or
  `poweroff` set, under a new counter *(unverified)*.

## 4. Events

The bridge notifies the radio packets it hears on `49550002-…`. It does not
notify the host's own writes. Each notification is 22 bytes:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | unknown, varies between packets (possibly signal strength) |
| 1 | 21 | radio packet (§2) |

Decode every 22-byte notification as a radio packet, whatever its first byte,
and ignore notifications of any other length.

A prop transmits a packet when its mode is changed on the prop: one per button
press, with the new page and mode. In packets from props `overrides` is 0 and
`lfo` holds other data.

## 5. Groups and counters

**Groups.** Props are addressed by a 16-bit `group_id`, shared by all the
props in a group. There is no known broadcast id: to address several groups,
send one packet per group. The five public groups are ids 1 to 5
*(unverified)*.

**Counters.** Each group has one counter, shared by its props and every
remote. Props apply a packet only if its `counter` is higher than the last one
they saw for the group. So:

1. Learn a group's counter from the packets the bridge notifies (§4).
2. Send each new state with the group's counter + 1, and remember it.
3. Send repeats (§3) with the same counter.

A host that doesn't know a counter may pick a much higher one. Props accept
it, but other remotes are then ignored until they catch up.

## 6. Discovering groups

There is no discovery request. A host learns a group, and its counter, from
the first packet notified for it (§4).

To make a prop announce its group, press its button **5 times quickly**. This
selects page 5, mode 1 (the prop's Grouping setting), and the prop transmits a
packet with `page=4`, `mode=0`. That page and mode are a setting, not a
pattern: don't treat them as the prop's pattern.

## 7. Examples

Complete payloads of single writes to `49550003-…` (the first two were
captured from the official app):

<!-- examples:start -->
| Packet | Bytes (hex) |
|---|---|
| Group `0x1F37`, page 1 mode 1, saturation and brightness 128, counter 5 | `1F 37 05 00 00 00 00 00 00 00 00 80 80 80 00 0D 00 00 00 00 08` |
| Group `0x1F37`, page 1 mode 2, saturation and brightness 128, counter 6 | `1F 37 06 00 00 00 00 00 00 00 00 80 80 80 00 0D 00 00 00 01 08` |
| Group `0x1F37`, page 2 mode 1, no overrides, counter 8 | `1F 37 08 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 00 08` |
| Group `0x1F37`, page 1 mode 3, hue 128, brightness 255, counter 9 | `1F 37 09 00 00 00 00 00 00 00 80 00 FF 00 00 0A 00 00 00 02 08` |
| Wake up group `0x1F37`, counter 10 | `1F 37 0A 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 02` |
| Power off group `0x1F37`, counter 11 | `1F 37 0B 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 04` |
<!-- examples:end -->

Notifications:

| Event | Notification (hex) |
|---|---|
| A prop announcing group `0x1F37` (counter 4) | `3E 1F 37 04 00 00 00 37 1F 04 00 00 00 00 00 00 00 00 00 04 00 08` |
| Mode changed to page 2 mode 5 on a prop (counter 1000273) | `29 1F 37 51 43 0F 00 00 00 01 04 00 00 00 00 00 00 00 00 01 04 08` |
