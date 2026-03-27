import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.session_manager import (
    SESSION_INACTIVITY_TIMEOUT,
    get_session_timeout_job_name,
    timeout_inactive_session,
    reset_session_timeout
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
