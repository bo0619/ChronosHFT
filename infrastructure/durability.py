"""Shared failure contract for durable runtime state."""


class DurabilityError(RuntimeError):
    """Base class for failures at a durable-state boundary."""
