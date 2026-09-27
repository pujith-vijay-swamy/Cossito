"""
Unit tests for state_machine.py.
"""

import unittest
from models import CodingTask, InvalidTransition, TaskState
from state_machine import TaskStateMachine, can_transition, is_terminal, transition


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.sm = TaskStateMachine()
        self.task = CodingTask()

    def test_initial_state(self):
        self.assertEqual(self.task.state, TaskState.QUEUED)
        self.assertFalse(is_terminal(self.task))

    def test_valid_forward_flow(self):
        # QUEUED -> RECONCILING -> READY -> PLANNING -> EDITING -> VALIDATING -> AWAITING_APPROVAL -> SUBMITTING -> VERIFYING -> PASSED
        transition(self.task, TaskState.RECONCILING)
        self.assertEqual(self.task.state, TaskState.RECONCILING)

        transition(self.task, TaskState.READY)
        self.assertEqual(self.task.state, TaskState.READY)

        transition(self.task, TaskState.PLANNING)
        self.assertEqual(self.task.state, TaskState.PLANNING)

        transition(self.task, TaskState.EDITING)
        self.assertEqual(self.task.state, TaskState.EDITING)

        transition(self.task, TaskState.VALIDATING)
        self.assertEqual(self.task.state, TaskState.VALIDATING)

        transition(self.task, TaskState.AWAITING_APPROVAL)
        self.assertEqual(self.task.state, TaskState.AWAITING_APPROVAL)

        transition(self.task, TaskState.SUBMITTING)
        self.assertEqual(self.task.state, TaskState.SUBMITTING)

        transition(self.task, TaskState.VERIFYING)
        self.assertEqual(self.task.state, TaskState.VERIFYING)

        transition(self.task, TaskState.PASSED)
        self.assertEqual(self.task.state, TaskState.PASSED)
        self.assertTrue(is_terminal(self.task))

    def test_invalid_transition_raises_error(self):
        # QUEUED cannot jump directly to PASSED
        with self.assertRaises(InvalidTransition):
            transition(self.task, TaskState.PASSED)

        # QUEUED cannot jump directly to EDITING
        with self.assertRaises(InvalidTransition):
            transition(self.task, TaskState.EDITING)

    def test_terminal_state_cannot_be_left(self):
        # Move to terminal PASSED
        self.task.state = TaskState.PASSED
        with self.assertRaises(InvalidTransition):
            transition(self.task, TaskState.PLANNING)

        with self.assertRaises(InvalidTransition):
            transition(self.task, TaskState.QUEUED)

    def test_repair_loop(self):
        # VERIFYING -> REPAIRING -> PLANNING -> EDITING ...
        self.task.state = TaskState.VERIFYING
        transition(self.task, TaskState.REPAIRING)
        self.assertEqual(self.task.state, TaskState.REPAIRING)

        transition(self.task, TaskState.PLANNING)
        self.assertEqual(self.task.state, TaskState.PLANNING)

        # Or REPAIRING -> FAILED (budget exhausted)
        self.task.state = TaskState.REPAIRING
        transition(self.task, TaskState.FAILED)
        self.assertEqual(self.task.state, TaskState.FAILED)
        self.assertTrue(is_terminal(self.task))

    def test_supervisory_transitions(self):
        # SKIPPED or CANCELLED from any non-terminal state
        for state in [TaskState.QUEUED, TaskState.PLANNING, TaskState.EDITING, TaskState.VERIFYING]:
            task = CodingTask()
            task.state = state
            self.assertTrue(can_transition(task, TaskState.SKIPPED))
            transition(task, TaskState.SKIPPED)
            self.assertEqual(task.state, TaskState.SKIPPED)
            self.assertTrue(is_terminal(task))

            task2 = CodingTask()
            task2.state = state
            self.assertTrue(can_transition(task2, TaskState.CANCELLED))
            transition(task2, TaskState.CANCELLED)
            self.assertEqual(task2.state, TaskState.CANCELLED)
            self.assertTrue(is_terminal(task2))


if __name__ == "__main__":
    unittest.main()
