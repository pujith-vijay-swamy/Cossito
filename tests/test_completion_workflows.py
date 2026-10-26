import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from models import CodingTask, SubmissionRecord, WorkspaceSnapshot
from controller_actions import build_controller
from verification import parse_test_results, is_submission_successful

class CompletionTests(unittest.IsolatedAsyncioTestCase):
    async def run_workflow(self, mode, started, result):
        task=CodingTask()
        orch=SimpleNamespace(control_flags={},_current_task=task,_log=Mock(),_notify=Mock(),
            state=SimpleNamespace(current_solution_code='', current_task_index=0),
            browser_mgr=SimpleNamespace(get_browser=AsyncMock(return_value=object())),
            _get_current_page_safe=AsyncMock(return_value=object()),_current_agent_task=None)
        adapter=SimpleNamespace(parse_result=AsyncMock(return_value=SubmissionRecord()),
            start_completion=AsyncMock(return_value={'mode':mode,'started':started,'controls':['Next Lesson']}))
        with patch('controller_actions._get_sym',side_effect=lambda name,fallback:fallback), \
             patch('controller_actions.detect_platform',new=AsyncMock(return_value=adapter)), \
             patch('controller_actions.safe_write',new=AsyncMock(return_value=(True,WorkspaceSnapshot(),''))), \
             patch('controller_actions.wait_for_terminal',new=AsyncMock(return_value=result)) as wait:
            action=build_controller(orch).registry.registry.actions['submit_code_solution'].function
            message=await action(code='<ul><li>Item</li></ul>')
        return orch,message,wait

    async def test_check_only_completion_is_verified(self):
        orch,message,wait=await self.run_workflow('validate',True,SubmissionRecord(terminal=True,outcome='accepted'))
        self.assertTrue(orch._task_completed_successfully)
        self.assertEqual(wait.call_args.kwargs['timeout'],30.0)

    async def test_next_only_with_fresh_completion_can_finish(self):
        record=parse_test_results('Lesson completed')
        self.assertTrue(is_submission_successful(record))
        orch,message,wait=await self.run_workflow('navigation',False,record)
        self.assertTrue(orch._task_completed_successfully)
        self.assertEqual(wait.call_args.kwargs['timeout'],3.0)

    async def test_next_only_without_evidence_does_not_wait_for_submit(self):
        orch,message,wait=await self.run_workflow('navigation',False,SubmissionRecord(terminal=False))
        self.assertIn('Do not wait for Submit',message)
        self.assertIsNone(orch._submission_started_task_id)
        self.assertFalse(getattr(orch,'_task_completed_successfully',False))
