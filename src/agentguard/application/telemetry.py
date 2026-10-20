"""Compatibility import; worker threads and delivery live in the adapter layer."""
from ..adapters.telemetry import EventDispatcher

__all__ = ["EventDispatcher"]
