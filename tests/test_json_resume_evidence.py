import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from llm_provider import InterceptingCompletions, RobustChatOpenAI, get_llm
from models import CodingTask, SubmissionRecord, AgentPhase
from verification import is_submission_successful, parse_test_results
from platform_adapters import wait_for_terminal


def response(text, finish='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text),finish_reason=finish)])

class JSONResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_fenced_json_preserves_code_exactly(self):
        value={'action':[{'write_code_to_editor':{'code':'<p title="hello">\\nline</p>'}}]}
        original=SimpleNamespace(create=AsyncMock(return_value=response('```json\n'+json.dumps(value)+'\n```')))
        result=await InterceptingCompletions(original).create(messages=[{'role':'system','content':'<json_schema>schema</json_schema>'}])
        self.assertEqual(json.loads(result.choices[0].message.content),value)
        original.create.assert_awaited_once()

    async def test_invalid_json_retries_once_without_browser_actions(self):
        original=SimpleNamespace(create=AsyncMock(side_effect=[response('{"action":['),response('{"action":[]}')]))
        result=await InterceptingCompletions(original).create(response_format={'type':'json_object'},messages=[])
        self.assertEqual(json.loads(result.choices[0].message.content),{'action':[]})
        self.assertEqual(original.create.await_count,2)

    async def test_persistent_invalid_output_does_not_fabricate_json(self):
        original=SimpleNamespace(create=AsyncMock(return_value=response('{"action":[')))
        with self.assertRaisesRegex(ValueError,'twice'):
            await InterceptingCompletions(original).create(response_format={'type':'json_object'},messages=[])
        self.assertEqual(original.create.await_count,2)

    async def test_plain_text_requests_are_unchanged(self):
        original=SimpleNamespace(create=AsyncMock(return_value=response('Hello')))
        result=await InterceptingCompletions(original).create(messages=[])
        self.assertEqual(result.choices[0].message.content,'Hello')

    def test_openrouter_uses_json_wrapper(self):
        self.assertIsInstance(get_llm(provider='OpenRouter',api_key='test'),RobustChatOpenAI)

class SubmissionEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_partial_counts_override_accepted_label(self):
        self.assertFalse(is_submission_successful(SubmissionRecord(terminal=True,outcome='accepted',passed_count=3,total_count=5,failed_count=2)))
        self.assertFalse(is_submission_successful(parse_test_results('Accepted 3/5 tests passed')))
        self.assertFalse(is_submission_successful(parse_test_results('Accepted Wrong Answer 5/5 tests passed')))

    async def test_unchanged_old_acceptance_never_advances(self):
        baseline=SubmissionRecord(terminal=True,outcome='accepted',raw_output='Accepted')
        adapter=SimpleNamespace(parse_result=AsyncMock(side_effect=lambda page:baseline.model_copy()))
        record=await wait_for_terminal(object(),adapter,timeout=.03,poll_interval=.001,baseline=baseline)
        self.assertFalse(record.terminal)
        self.assertTrue(baseline.terminal)

    async def test_busy_then_same_verdict_is_a_new_result(self):
        baseline=SubmissionRecord(terminal=True,outcome='accepted',raw_output='Accepted')
        adapter=SimpleNamespace(parse_result=AsyncMock(side_effect=[SubmissionRecord(terminal=False),baseline.model_copy()]))
        record=await wait_for_terminal(object(),adapter,timeout=.1,poll_interval=.001,baseline=baseline)
        self.assertTrue(record.terminal)

    async def test_model_report_cannot_claim_unverified_success(self):
        from controller_actions import build_controller
        orch=SimpleNamespace(control_flags={},_code_written_in_current_task=True,_task_completed_successfully=False)
        action=build_controller(orch).registry.registry.actions['report_task_result'].function
        text=await action(task_id=1,success=True,output='Accepted')
        self.assertIn('Cannot report success',text)
        self.assertFalse(orch._task_completed_successfully)

class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_debugger_setup_does_not_modify_website_javascript(self):
        from agent_core import AgentOrchestrator
        import asyncio
        cdp=SimpleNamespace(send_raw=AsyncMock())
        page=SimpleNamespace(session_id=asyncio.sleep(0, result='session'))
        await AgentOrchestrator._disable_cdp_debugger_pauses(object(),SimpleNamespace(_cdp_client_root=cdp),page)
        calls=[call.args[0] for call in cdp.send_raw.await_args_list]
        self.assertEqual(calls,['Debugger.enable','Debugger.setSkipAllPauses','Debugger.setBreakpointsActive'])

    async def test_resume_uses_existing_workspace_without_navigating_blank_tab(self):
        from agent_core import AgentOrchestrator
        orch=AgentOrchestrator(url='https://example.test/task',api_key='test',headless=True)
        orch.state.phase=AgentPhase.EXECUTING
        blank=SimpleNamespace(get_url=AsyncMock(return_value='about:blank'),goto=AsyncMock())
        workspace=SimpleNamespace(target_id='workspace-tab',get_url=AsyncMock(return_value=orch.url),goto=AsyncMock())
        browser=SimpleNamespace(is_cdp_connected=True,get_current_page=AsyncMock(return_value=blank),get_pages=AsyncMock(return_value=[blank,workspace]),get_or_create_cdp_session=AsyncMock())
        page=await orch._get_current_page_safe(browser)
        self.assertIs(page,workspace)
        blank.goto.assert_not_awaited()
        workspace.goto.assert_not_awaited()
        browser.get_or_create_cdp_session.assert_awaited_once_with('workspace-tab',focus=True)
