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

def build_trigger_keyboard(trigger_type: str) -> InlineKeyboardMarkup:
    """Builds the Start/Delay inline keyboard for scheduled triggers."""
    keyboard = [
        [InlineKeyboardButton("Start", callback_data=f"start_trigger_{trigger_type}")],
        [InlineKeyboardButton("Not Now / Chat First", callback_data="delay_trigger")]
    ]
    return InlineKeyboardMarkup(keyboard)
