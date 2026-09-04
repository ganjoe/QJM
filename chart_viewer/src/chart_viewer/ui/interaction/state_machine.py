"""Interaction State Machine according to Section 7."""

from __future__ import annotations
from enum import Enum, auto
from typing import Optional


class InteractionState(Enum):
    IDLE = auto()
    MEASURING = auto()
    DRAGGING_ANNOTATION = auto()
    PANNING = auto()
    SCALING_Y = auto()
    ZOOMING_X = auto()


class InteractionStateMachine:
    """Manages modal interaction state transitions."""

    def __init__(self):
        self.state: InteractionState = InteractionState.IDLE
        self.active_annotation_id: Optional[str] = None
        self.active_anchor_index: Optional[int] = None

    def start_measuring(self) -> None:
        if self.state == InteractionState.IDLE:
            self.state = InteractionState.MEASURING

    def start_dragging_annotation(self, annotation_id: str, anchor_index: Optional[int] = None) -> None:
        if self.state == InteractionState.IDLE:
            self.state = InteractionState.DRAGGING_ANNOTATION
            self.active_annotation_id = annotation_id
            self.active_anchor_index = anchor_index

    def start_panning(self) -> None:
        if self.state == InteractionState.IDLE:
            self.state = InteractionState.PANNING

    def cancel(self) -> None:
        """Escape key cancels active tool immediately (Section 7 highest priority)."""
        self.state = InteractionState.IDLE
        self.active_annotation_id = None
        self.active_anchor_index = None

    def release(self) -> tuple[InteractionState, Optional[str]]:
        """Handles mouse release, returning prior state and touched annotation."""
        prior = self.state
        ann_id = self.active_annotation_id
        self.state = InteractionState.IDLE
        self.active_annotation_id = None
        self.active_anchor_index = None
        return prior, ann_id
