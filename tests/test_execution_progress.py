import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from models import CodingTask
from task_subagent import TaskSubagent


class ExecutionProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.task = CodingTask()
        self.task.description = 'Old task description that must be replaced before planning.'
        self.statement = 'Create an unordered list containing the items specified in this exercise.'
        self.orch = SimpleNamespace(
            state=SimpleNamespace(model_thinking=None), control_flags={},
            browser_mgr=SimpleNamespace(get_browser=AsyncMock(return_value=object())),
            _get_current_page_safe=AsyncMock(return_value=object()),
            _extract_problem_from_dom_direct=AsyncMock(return_value=(self.statement, [], [])),
            _get_llm_instance=Mock(return_value=object()), _build_controller=Mock(),
            _is_frontend_task=Mock(return_value=False), _log=Mock(),
            url='https://example.test/task', use_vision=False, _on_agent_step=AsyncMock(),
        )

    async def test_fresh_statement_reaches_model_and_timeout_is_reported_without_browser_recovery(self):
        history = SimpleNamespace(errors=Mock(return_value=['Model request timed out']))
        agent = SimpleNamespace(run=AsyncMock(return_value=history))
        perception = Mock()
        with patch('task_subagent.Agent', return_value=agent) as factory:
            result = await TaskSubagent(self.task, self.orch, perception).execute()
        self.assertFalse(result.success)
        self.assertEqual(result.output_message, 'Model request timed out')
        self.assertIn(self.statement, factory.call_args.kwargs['task'])
        self.assertNotIn('Old task description', factory.call_args.kwargs['task'])
        self.assertEqual(factory.call_args.kwargs['max_failures'], 1)
        perception.capture_multimodal_observation.assert_not_called()
        self.orch._get_current_page_safe.assert_awaited_once()
        hooks = agent.run.call_args.kwargs
        await hooks['on_step_end'](SimpleNamespace(state=SimpleNamespace(last_result=[SimpleNamespace(error='Model request timed out')])))
        self.orch._log.assert_called_with('Execution step failed: Model request timed out', 'error')

    async def test_missing_statement_does_not_launch_an_agent(self):
        self.orch._extract_problem_from_dom_direct.return_value = ('Loading your task, please wait.', [], [])
        with patch('task_subagent.Agent') as factory:
            result = await TaskSubagent(self.task, self.orch).execute()
        self.assertFalse(result.success)
        self.assertIn('description is unavailable', result.output_message)
        factory.assert_not_called()

    async def test_no_progress_has_explicit_failure_reason(self):
        agent = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(errors=Mock(return_value=[]))))
        with patch('task_subagent.Agent', return_value=agent):
            result = await TaskSubagent(self.task, self.orch).execute()
        self.assertIn('without writing code', result.output_message)

    async def test_written_code_and_model_done_do_not_replace_submission_verification(self):
        history = SimpleNamespace(errors=Mock(return_value=[]), has_errors=Mock(return_value=False),
                                  is_done=Mock(return_value=True), final_result=Mock(return_value='Accepted, all tests passed'))
        async def run(**kwargs):
            self.orch._code_written_in_current_task = True
            return history
        perception = SimpleNamespace(clear_telemetry=Mock(), capture_multimodal_observation=AsyncMock(return_value=None))
        with patch('task_subagent.Agent', return_value=SimpleNamespace(run=AsyncMock(side_effect=run))):
            result = await TaskSubagent(self.task, self.orch, perception).execute()
        self.assertFalse(result.success)
        self.assertIn('did not confirm passing', result.output_message)

    async def test_json_error_after_writing_does_not_trigger_page_recovery(self):
        async def run(**kwargs):
            self.orch._code_written_in_current_task = True
            return SimpleNamespace(errors=Mock(return_value=['json_invalid: incomplete response']))
        perception = Mock()
        with patch('task_subagent.Agent', return_value=SimpleNamespace(run=AsyncMock(side_effect=run))):
            result = await TaskSubagent(self.task, self.orch, perception).execute()
        self.assertFalse(result.success)
        self.assertIn('json_invalid', result.output_message)
        perception.capture_multimodal_observation.assert_not_called()

    async def test_orchestrator_retains_failure_reason_for_pause_instead_of_code_repair(self):
        from agent_core import AgentOrchestrator
        from models import TaskExecutionResult
        self.orch.perception_engine = Mock()
        self.orch._code_written_in_current_task = False
        result = TaskExecutionResult(task_id=self.task.task_id, success=False, output_message='Model request timed out')
        with patch('agent_core.TaskSubagent') as factory:
            factory.return_value.execute = AsyncMock(return_value=result)
            passed = await AgentOrchestrator._solve_and_verify(self.orch, self.task)
        self.assertFalse(passed)
        self.assertEqual(self.orch._execution_blocked_reason, 'Model request timed out')

    async def test_no_progress_does_not_restart_or_spend_code_repair_budget(self):
        from agent_core import AgentOrchestrator
        orch = AgentOrchestrator(url='https://example.test/task', api_key='test', headless=True)
        orch._notify = Mock()
        orch._log = Mock()
        orch._navigate_to_task_question = AsyncMock()
        page = SimpleNamespace(get_url=AsyncMock(return_value=orch.url), evaluate=AsyncMock(return_value=[]))
        orch.browser_mgr.get_browser = AsyncMock(return_value=object())
        orch._get_current_page_safe = AsyncMock(return_value=page)
        orch._extract_problem_from_dom_direct = AsyncMock(return_value=(self.statement, [], []))
        async def blocked(*args, **kwargs):
            orch._execution_blocked_reason = 'Model request timed out'
            return False
        orch._solve_and_verify = AsyncMock(side_effect=blocked)
        async def stop_after_pause(reason):
            orch.control_flags['stop_requested'] = True
        orch.hitl.trigger_pause = AsyncMock(side_effect=stop_after_pause)
        before = self.task.budgets.code_repairs
        with patch('agent_core.wait_for_page_ready', new=AsyncMock()):
            await orch._execute_single_task(self.task)
        orch._solve_and_verify.assert_awaited_once()
        orch.hitl.trigger_pause.assert_awaited_once()
        self.assertEqual(self.task.budgets.code_repairs, before)
        self.assertIn('Model request timed out', orch.state.hitl_reason)
