from __future__ import annotations

from typing import Set

from models import TaskState, CodingTask, InvalidTransition

class TaskStateMachine:
    """State machine for task transitions."""
    VALID_TRANSITIONS = {
        TaskState.QUEUED: {TaskState.RECONCILING},
        TaskState.RECONCILING: {TaskState.READY, TaskState.NEEDS_INPUT},
        TaskState.READY: {TaskState.PLANNING},
        TaskState.PLANNING: {TaskState.EDITING},
        TaskState.EDITING: {TaskState.VALIDATING},
        TaskState.VALIDATING: {TaskState.AWAITING_APPROVAL},
        TaskState.AWAITING_APPROVAL: {TaskState.SUBMITTING},
        TaskState.SUBMITTING: {TaskState.VERIFYING, TaskState.RECONCILING_SUBMISSION},
        TaskState.RECONCILING_SUBMISSION: {TaskState.VERIFYING, TaskState.NEEDS_INPUT},
        TaskState.VERIFYING: {TaskState.PASSED, TaskState.REPAIRING, TaskState.NEEDS_INPUT},
        TaskState.REPAIRING: {TaskState.PLANNING, TaskState.FAILED},
        TaskState.NEEDS_INPUT: {TaskState.RECONCILING},
        TaskState.PASSED: set(),
        TaskState.FAILED: set(),
        TaskState.SKIPPED: set(),
        TaskState.CANCELLED: set(),
    }
    
    SUPERVISORY: set[TaskState] = {TaskState.SKIPPED, TaskState.CANCELLED}
    TERMINAL: set[TaskState] = {TaskState.PASSED, TaskState.FAILED, TaskState.SKIPPED, TaskState.CANCELLED}

    def can_transition(self, task: CodingTask, target: TaskState) -> bool:
        """Check if a transition is valid."""
        if self.is_terminal(task):
            return False
            
        if target in self.SUPERVISORY:
            return True
            
        valid_targets = self.VALID_TRANSITIONS.get(task.state, set())
        return target in valid_targets

    def transition(self, task: CodingTask, target: TaskState, evidence: str = '') -> None:
        """Apply a transition to a task."""
        if not self.can_transition(task, target):
            raise InvalidTransition(f"Cannot transition from {task.state.name} to {target.name}")
            
        task.state = target
        # Optional: Store evidence if the model has a way to handle it.
        # But we must just transition. We can just pass for now.

    def is_terminal(self, task: CodingTask) -> bool:
        """Check if the task is in a terminal state."""
        return task.state in self.TERMINAL

    def available_transitions(self, task: CodingTask) -> set[TaskState]:
        """Get the available transitions for a task."""
        if self.is_terminal(task):
            return set()
            
        transitions = self.VALID_TRANSITIONS.get(task.state, set()).copy()
        transitions.update(self.SUPERVISORY)
        return transitions

_default_machine = TaskStateMachine()

def transition(task: CodingTask, target: TaskState, evidence: str = '') -> None:
    """Transition a task to a new state."""
    _default_machine.transition(task, target, evidence)

def can_transition(task: CodingTask, target: TaskState) -> bool:
    """Check if a transition is valid."""
    return _default_machine.can_transition(task, target)

def is_terminal(task: CodingTask) -> bool:
    """Check if the task is in a terminal state."""
    return _default_machine.is_terminal(task)
