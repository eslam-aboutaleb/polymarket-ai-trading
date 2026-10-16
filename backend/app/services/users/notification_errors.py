"""Transport-independent errors shared by notification delivery components."""


class AlertDeliveryError(Exception):
    """Raised when a single alert delivery attempt cannot proceed."""
