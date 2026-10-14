"""Exceptions raised by the flowtoys library."""


class FlowtoysError(Exception):
    """Base class for all flowtoys errors."""


class NotABridgeError(FlowtoysError):
    """The connected device does not expose the bridge's command characteristic."""


class PayloadTooLargeError(FlowtoysError):
    """A packet does not fit in a single BLE write on the current connection."""


class UnknownGroupError(FlowtoysError):
    """A command targets a group whose current packet counter is not known."""
