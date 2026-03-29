class QueueFullError(Exception):
    """Event queue is at capacity."""


class ServiceUnavailableError(Exception):
    """A required backend service is unreachable."""
