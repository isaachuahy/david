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

def build_weekly_state_keyboard() -> InlineKeyboardMarkup:
    """Builds the Confirm/Reject inline keyboard for weekly state updates."""
    keyboard = [
        [
            InlineKeyboardButton("Confirm", callback_data="confirm_weekly_state"),
            InlineKeyboardButton("Reject", callback_data="reject_weekly_state")
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

def build_artifact_write_retry_keyboard(write_id: str) -> InlineKeyboardMarkup:
    """Builds a retry keyboard for one failed confirmed artifact write."""
    keyboard = [
        [
            InlineKeyboardButton("Retry Write", callback_data=f"retry_artifact_write_{write_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_trigger_keyboard(trigger_type: str) -> InlineKeyboardMarkup:
    """Builds the Start/Delay inline keyboard for scheduled triggers."""
    keyboard = [
        [InlineKeyboardButton("Start", callback_data=f"start_trigger_{trigger_type}")],
        [InlineKeyboardButton("Not Now / Chat First", callback_data="delay_trigger")]
    ]
    return InlineKeyboardMarkup(keyboard)
