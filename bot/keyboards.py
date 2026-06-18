from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def build_proposal_item_keyboard(item_id: str) -> InlineKeyboardMarkup:
    """Builds the Confirm/Reject inline keyboard for one proposal item."""
    keyboard = [
        [
            InlineKeyboardButton("Confirm", callback_data=f"confirm_item_{item_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject_item_{item_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_review_stage_keyboard(stage: str) -> InlineKeyboardMarkup:
    """
    Builds the Confirm/Revise inline keyboard for a Sunday review stage.

    The stage is embedded in the callback so one generic handler can confirm or
    revise week review, goals audit, memory audit, and weekly plan gates.
    """
    keyboard = [
        [
            InlineKeyboardButton("Confirm", callback_data=f"confirm_review_stage_{stage}"),
            InlineKeyboardButton("Revise", callback_data=f"reject_review_stage_{stage}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_review_resume_keyboard(review_id: str) -> InlineKeyboardMarkup:
    """
    Builds the startup recovery keyboard for one durable Sunday review.

    The review id is embedded so a restart can recover volatile Telegram state
    from the persisted ReviewWorkflowRecord, or deliberately mark the stale
    review failed so it stops resurfacing on future restarts.
    """
    keyboard = [
        [InlineKeyboardButton("Continue Review", callback_data=f"resume_review_{review_id}")],
        [InlineKeyboardButton("Discard Review", callback_data=f"discard_review_{review_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)

def build_artifact_write_retry_keyboard(write_id: str) -> InlineKeyboardMarkup:
    """Builds a retry keyboard for one failed confirmed artifact write."""
    keyboard = [
        [
            InlineKeyboardButton("Retry Write", callback_data=f"retry_artifact_write_{write_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_trigger_keyboard(trigger_type: str) -> InlineKeyboardMarkup:
    """
    Builds the controls shared by every scheduled trigger prompt.

    Clearing is intentionally available alongside both daily and weekly review
    prompts because either prompt may be the visible front of a larger queue.
    """
    keyboard = [
        [InlineKeyboardButton("Start", callback_data=f"start_trigger_{trigger_type}")],
        [InlineKeyboardButton("Not Now / Chat First", callback_data="delay_trigger")],
        [InlineKeyboardButton("Clear Queue", callback_data="clear_trigger_queue")],
    ]
    return InlineKeyboardMarkup(keyboard)
