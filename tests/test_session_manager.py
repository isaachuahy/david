import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.session_manager import (
    SESSION_INACTIVITY_TIMEOUT,
    get_session_timeout_job_name,
    timeout_inactive_session,
    reset_session_timeout,
    execute_synthesis_task,
)
from persistence.models import SessionStatus

def test_reset_session_timeout_replaces_existing_job():
    context = MagicMock()
    existing_job = MagicMock()
    context.job_queue.get_jobs_by_name.return_value = (existing_job,)

    reset_session_timeout(context, chat_id=456, user_id=123)

    existing_job.schedule_removal.assert_called_once()
    context.job_queue.run_once.assert_called_once_with(
        timeout_inactive_session,
        SESSION_INACTIVITY_TIMEOUT,
        data={"chat_id": 456},
        name=get_session_timeout_job_name(123),
        chat_id=456,
        user_id=123,
    )

@pytest.mark.asyncio
@patch('orchestrator.session_manager.end_session', new_callable=AsyncMock)
async def test_timeout_inactive_session_closes_active_session(mock_end_session):
    context = MagicMock()
    context.user_data = {"session_state": SessionStatus.ACTIVE}
    context.job.data = {"chat_id": 456}

    await timeout_inactive_session(context)

    mock_end_session.assert_awaited_once_with(context, 456, reason="timeout")

@pytest.mark.asyncio
@patch('orchestrator.session_manager.prompt_next_trigger', new_callable=AsyncMock)
@patch('orchestrator.session_manager.append_to_decision_log')
@patch('orchestrator.session_manager.generate_session_synthesis')
async def test_execute_synthesis_task_appends_and_finalizes_state(
    mock_generate_session_synthesis,
    mock_append_to_decision_log,
    mock_prompt_next_trigger,
):
    context = MagicMock()
    context.user_data = {
        "chat_history": [{"role": "user", "content": "We decided to focus on sales."}],
        "cached_events": [{"summary": "Stale cached event"}],
        "session_state": SessionStatus.CLOSING,
        "current_session_id": "sess_123",
    }
    context.job.data = {"chat_id": 456, "session_id": "sess_123"}
    context.bot.send_message = AsyncMock()

    mock_generate_session_synthesis.return_value = MagicMock(content="### Session - 2026-03-30\n- Focused on sales.")

    await execute_synthesis_task(context)

    mock_generate_session_synthesis.assert_called_once()
    args = mock_generate_session_synthesis.call_args.args
    kwargs = mock_generate_session_synthesis.call_args.kwargs
    assert args[0] == [{"role": "user", "content": "We decided to focus on sales."}]
    assert kwargs["session_date"]
    mock_append_to_decision_log.assert_called_once_with("### Session - 2026-03-30\n- Focused on sales.")
    assert context.user_data["chat_history"] == []
    assert "cached_events" not in context.user_data
    assert context.user_data["session_state"] == SessionStatus.IDLE
    assert context.user_data["current_session_id"] is None
    mock_prompt_next_trigger.assert_awaited_once_with(context, 456)
    context.bot.send_message.assert_not_awaited()

@pytest.mark.asyncio
@patch('orchestrator.session_manager.prompt_next_trigger', new_callable=AsyncMock)
@patch('orchestrator.session_manager.append_to_decision_log')
@patch('orchestrator.session_manager.generate_session_synthesis', side_effect=Exception("boom"))
async def test_execute_synthesis_task_notifies_on_failure_and_finalizes_state(
    mock_generate_session_synthesis,
    mock_append_to_decision_log,
    mock_prompt_next_trigger,
):
    context = MagicMock()
    context.user_data = {
        "chat_history": [{"role": "user", "content": "Important decision"}],
        "cached_events": [{"summary": "Stale cached event"}],
        "session_state": SessionStatus.CLOSING,
        "current_session_id": "sess_456",
    }
    context.job.data = {"chat_id": 789, "session_id": "sess_456"}
    context.bot.send_message = AsyncMock()

    await execute_synthesis_task(context)

    mock_generate_session_synthesis.assert_called_once()
    mock_append_to_decision_log.assert_not_called()
    context.bot.send_message.assert_awaited_once_with(
        chat_id=789,
        text="⚠️ I closed the session, but failed to update the decision log. Please check the logs."
    )
    assert context.user_data["chat_history"] == []
    assert "cached_events" not in context.user_data
    assert context.user_data["session_state"] == SessionStatus.IDLE
    assert context.user_data["current_session_id"] is None
    mock_prompt_next_trigger.assert_awaited_once_with(context, 789)
