from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def build_confirmation_keyboard(write_id: str) -> InlineKeyboardMarkup:
    """Builds the Confirm/Reject inline keyboard for calendar proposals."""
    keyboard = [
        [
            InlineKeyboardButton("Confirm", callback_data=f"confirm_{write_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject_{write_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)
