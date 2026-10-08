"""Authenticate context-editor requests using Telegram's signed Mini App data.

The HTTP layer must pass raw Telegram.WebApp.initData on every context request.
This module does not trust initDataUnsafe, load credentials, or log launch data.
Telegram's bot-token validation rules were checked on 2026-10-08:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


DEFAULT_MAX_AGE_SECONDS = 3600
MAX_FUTURE_SKEW_SECONDS = 30
MAX_INIT_DATA_LENGTH = 16_384


class ContextEditorAuthError(ValueError):
    """Reject a request before the HTTP layer reads or changes private context."""


def validate_init_data(
    init_data: str,
    *,
    bot_token: str,
    allowed_user_id: int,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: int | None = None,
) -> int:
    """Return the allowed user's ID after verifying the signature and launch age.

    Configuration and the optional test clock come from the trusted server,
    never from request parameters. Invalid server settings raise ValueError;
    rejected launch data raises ContextEditorAuthError without exposing it.
    The one-hour default is editor policy, not a Telegram-mandated timeout.
    """
    if not isinstance(bot_token, str) or not bot_token:
        raise ValueError("The context editor requires a bot token.")
    if type(allowed_user_id) is not int or allowed_user_id <= 0:
        raise ValueError("The context editor requires a positive allowed_user_id.")
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be a positive integer.")
    if now is not None and type(now) is not int:
        raise ValueError("The trusted authentication clock must use integer Unix seconds.")
    if not isinstance(init_data, str) or not init_data or len(init_data) > MAX_INIT_DATA_LENGTH:
        raise ContextEditorAuthError("Open the editor from David in Telegram.")

    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=32,
        )
    except ValueError as error:
        raise ContextEditorAuthError("Invalid Telegram authentication data.") from error

    fields: dict[str, str] = {}
    for key, value in pairs:
        # Each signed field must have one meaning. Duplicate keys and literal
        # line breaks could otherwise make the signature's field list ambiguous.
        if (
            not key
            or key in fields
            or any(character in key or character in value for character in "\r\n")
        ):
            raise ContextEditorAuthError("Invalid Telegram authentication data.")
        fields[key] = value

    received_hash = fields.pop("hash", "")
    try:
        received_digest = bytes.fromhex(received_hash)
    except ValueError as error:
        raise ContextEditorAuthError("Invalid Telegram authentication signature.") from error
    if len(received_hash) != 64 or len(received_digest) != 32:
        raise ContextEditorAuthError("Invalid Telegram authentication signature.")

    # Telegram's bot-token HMAC covers every field except hash, including the
    # optional signature field used by its separate third-party validation flow.
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    try:
        data_check_bytes = data_check_string.encode("utf-8")
    except UnicodeError as error:
        raise ContextEditorAuthError("Invalid Telegram authentication data.") from error
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_digest = hmac.new(
        secret_key, data_check_bytes, hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected_digest, received_digest):
        raise ContextEditorAuthError("Invalid Telegram authentication signature.")

    raw_auth_date = fields.get("auth_date", "")
    try:
        if not raw_auth_date.isascii() or not raw_auth_date.isdecimal():
            raise ValueError("auth_date is not an integer timestamp.")
        auth_date = int(raw_auth_date)
    except ValueError as error:
        raise ContextEditorAuthError("Invalid Telegram authentication date.") from error
    current_time = int(time.time()) if now is None else now
    age = current_time - auth_date
    if auth_date <= 0 or age < -MAX_FUTURE_SKEW_SECONDS:
        raise ContextEditorAuthError("Invalid Telegram authentication date.")
    if age > max_age_seconds:
        raise ContextEditorAuthError("Your editor session has expired. Reopen it from Telegram.")

    try:
        user = json.loads(fields["user"])
    except (KeyError, ValueError) as error:
        raise ContextEditorAuthError("Invalid Telegram user data.") from error
    if not isinstance(user, dict) or type(user.get("id")) is not int:
        raise ContextEditorAuthError("Invalid Telegram user data.")
    if user["id"] != allowed_user_id:
        raise ContextEditorAuthError("This editor is not available to this Telegram user.")
    return user["id"]
