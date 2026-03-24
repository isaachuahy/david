from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def build_calendar_confirmation_keyboard(write_id: str) -> InlineKeyboardMarkup:
    """Builds the Confirm/Reject inline keyboard for calendar proposals."""
    keyboard = [
        [
            InlineKeyboardButton("Confirm", callback_data=f"confirm_{write_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject_{write_id}")
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
