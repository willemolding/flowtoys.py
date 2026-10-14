"""Control FlowToys props through a Flowtoys Bridge over Bluetooth LE."""

from importlib.metadata import version

from ._protocol import COMMAND_CHAR_UUID, EVENT_CHAR_UUID, SERVICE_UUID
from .bridge import Bridge
from .discovery import discover_bridges
from .exceptions import FlowtoysError, NotABridgeError, PayloadTooLargeError, UnknownGroupError
from .models import Flags, Mode, Overrides, Packet

__version__ = version("flowtoys")

__all__ = [
    "COMMAND_CHAR_UUID",
    "EVENT_CHAR_UUID",
    "SERVICE_UUID",
    "Bridge",
    "Flags",
    "FlowtoysError",
    "Mode",
    "NotABridgeError",
    "Overrides",
    "Packet",
    "PayloadTooLargeError",
    "UnknownGroupError",
    "__version__",
    "discover_bridges",
]
