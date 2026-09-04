"""Core package."""

from chart_viewer.core.event_hub import EventHub
from chart_viewer.core.backpressure import TickCoalescer
from chart_viewer.core.state_manager import StateManager, WindowData

__all__ = [
    "EventHub",
    "TickCoalescer",
    "StateManager",
    "WindowData",
]
