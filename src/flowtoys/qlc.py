"""The QLC+ fixture definition for a group of props, generated from :mod:`flowtoys.dmx`.

Save it with ``flowtoys --write-fixture PATH`` and copy it to QLC+'s fixtures
folder, then patch one fixture per group of props.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from .dmx import EXTENDED_CHANNELS, PATTERNS, STANDARD_CHANNELS

FILE_NAME = "Flowtoys-Bridge-Group.qxf"

_HUES = [
    (0, 10, "Red", "#ff0000"),
    (11, 31, "Orange", "#ffa500"),
    (32, 52, "Yellow", "#ffff00"),
    (53, 74, "Chartreuse", "#7fff00"),
    (75, 95, "Green", "#00ff00"),
    (96, 116, "Spring green", "#00ff7f"),
    (117, 138, "Cyan", "#00ffff"),
    (139, 159, "Dodger blue", "#1e90ff"),
    (160, 180, "Blue", "#0000ff"),
    (181, 202, "Blue violet", "#8a2be2"),
    (203, 223, "Magenta", "#ff00ff"),
    (224, 244, "Rose", "#ff0080"),
    (245, 255, "Red", "#ff0000"),
]

# name in QLC+, channel group, capabilities as (min, max, label[, colour])
_Capability = tuple[int, int, str] | tuple[int, int, str, str]
_CHANNELS: dict[str, tuple[str, str, list[_Capability]]] = {
    "pattern": (
        "Pattern",
        "Effect",
        [(0, 0, "No change"), *((v, v, p.label) for v, p in enumerate(PATTERNS, start=1))],
    ),
    "brightness": ("Brightness", "Intensity", []),
    "hue": ("Hue", "Colour", list(_HUES)),
    "saturation": ("Saturation", "Colour", [(0, 255, "Saturation (white to full colour)")]),
    "speed": ("Speed", "Speed", [(0, 255, "Pattern speed")]),
    "density": ("Density", "Effect", [(0, 255, "Pattern density")]),
    "retrigger": ("Retrigger", "Effect", [(0, 127, "Idle"), (128, 255, "Retrigger pattern")]),
    **{f"lfo{n}": (f"LFO {n}", "Effect", [(0, 255, f"LFO {n}")]) for n in range(1, 5)},
}


def fixture_definition() -> str:
    """The fixture definition (``.qxf``) with Standard and Extended modes."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!DOCTYPE FixtureDefinition>",
        '<FixtureDefinition xmlns="http://www.qlcplus.org/FixtureDefinition">',
        " <Creator>",
        "  <Name>Q Light Controller Plus</Name>",
        "  <Version>4.13.0</Version>",
        "  <Author>Willem Olding</Author>",
        " </Creator>",
        " <Manufacturer>Flowtoys</Manufacturer>",
        " <Model>Bridge Group</Model>",
        " <Type>Effect</Type>",
    ]
    for key in EXTENDED_CHANNELS:
        name, group, capabilities = _CHANNELS[key]
        if key == "brightness":
            lines.append(f' <Channel Name="{name}" Preset="IntensityMasterDimmer"/>')
            continue
        lines.append(f' <Channel Name="{name}">')
        lines.append(f'  <Group Byte="0">{group}</Group>')
        for low, high, label, *colour in capabilities:
            preset = f' Preset="ColorMacro" Res1="{colour[0]}"' if colour else ""
            lines.append(
                f'  <Capability Min="{low}" Max="{high}"{preset}>{escape(label)}</Capability>'
            )
        lines.append(" </Channel>")
    for mode, channels in (("Standard", STANDARD_CHANNELS), ("Extended", EXTENDED_CHANNELS)):
        lines.append(f' <Mode Name="{mode}">')
        for number, key in enumerate(channels):
            lines.append(f'  <Channel Number="{number}">{_CHANNELS[key][0]}</Channel>')
        lines.append(" </Mode>")
    lines += [
        " <Physical>",
        '  <Bulb Type="LED" Lumens="0" ColourTemperature="0"/>',
        '  <Dimensions Weight="0.1" Width="60" Height="25" Depth="40"/>',
        '  <Lens Name="Other" DegreesMin="0" DegreesMax="0"/>',
        '  <Focus Type="Fixed" PanMax="0" TiltMax="0"/>',
        '  <Technical PowerConsumption="1" DmxConnector="Other"/>',
        " </Physical>",
        "</FixtureDefinition>",
    ]
    return "\n".join(lines) + "\n"
