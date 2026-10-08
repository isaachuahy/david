"""Verify Mini App authentication with synthetic credentials and launch data."""

import hmac
import json
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode

import pytest

from bot.context_editor_auth import ContextEditorAuthError, validate_init_data


BOT_TOKEN = "12345:synthetic-test-token"
USER_ID = 4_503_599_627_370_495
NOW = 1_791_489_600
BASE_FIELDS = {
    "auth_date": str(NOW),
    "query_id": "synthetic-query",
    "user": '{"id":4503599627370495,"first_name":"Café + focus"}',
}


def _signed_data(fields=None, *, token=BOT_TOKEN):
    """Sign synthetic fields using Telegram's documented HMAC key order."""
    fields = BASE_FIELDS if fields is None else fields
    secret = hmac.digest(b"WebAppData", token.encode("utf-8"), "sha256")
    message = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    digest = hmac.digest(secret, message.encode("utf-8"), "sha256").hex()
    return urlencode([*reversed(list(fields.items())), ("hash", digest)])


def _validate(data, **settings):
    """Validate with a fixed trusted server clock and no application credentials."""
    arguments = {
        "bot_token": BOT_TOKEN,
        "allowed_user_id": USER_ID,
        "now": NOW,
        **settings,
    }
    return validate_init_data(data, **arguments)


def test_documented_hmac_reference_vector():
    """Check a fixed reference independently of this file's Python signer."""
    # OpenSSL generated this synthetic hash to avoid sharing a key-order bug
    # between the validation module and the test-data generator.
    reference_hash = "c2ca90c533adf451fe71fe1b947bf7cf87a68df2dc554ce7ef0771c3ac08a897"
    data = urlencode({**BASE_FIELDS, "hash": reference_hash})

    assert _validate(data) == USER_ID


@pytest.mark.parametrize(
    "extra_fields",
    [
        {},
        {"signature": "synthetic-third-party-signature"},
        {"start_param": "literal%text+with=separators"},
        {"future_field": "new=value", "empty_field": ""},
    ],
)
def test_valid_launch_preserves_encoded_values_and_optional_fields(extra_fields):
    """Field order, Unicode, and optional signed data do not block the allowed user."""
    assert _validate(_signed_data({**BASE_FIELDS, **extra_fields})) == USER_ID


def test_default_clock_uses_server_time():
    """Validation uses the server clock when the caller supplies no test clock."""
    with patch("bot.context_editor_auth.time.time", return_value=NOW):
        assert validate_init_data(
            _signed_data(), bot_token=BOT_TOKEN, allowed_user_id=USER_ID,
        ) == USER_ID


@pytest.mark.parametrize("age, max_age", [(0, 3600), (3600, 3600), (-30, 3600), (90, 90)])
def test_launch_age_accepts_policy_boundaries(age, max_age):
    """The configured editing window and clock tolerance include their boundaries."""
    data = _signed_data({**BASE_FIELDS, "auth_date": str(NOW - age)})

    assert _validate(data, max_age_seconds=max_age) == USER_ID


@pytest.mark.parametrize(
    "age, max_age, message",
    [(3601, 3600, "expired"), (-31, 3600, "authentication date"), (91, 90, "expired")],
)
def test_launch_age_rejects_expired_or_future_data(age, max_age, message):
    """A genuine signature cannot bypass the editor's launch-age policy."""
    data = _signed_data({**BASE_FIELDS, "auth_date": str(NOW - age)})

    with pytest.raises(ContextEditorAuthError, match=message):
        _validate(data, max_age_seconds=max_age)


@pytest.mark.parametrize("field", ["user", "auth_date", "query_id", "signature"])
def test_tampering_with_any_signed_field_is_rejected(field):
    """The bot-token signature also protects Telegram's optional signature field."""
    fields = dict(parse_qsl(_signed_data({**BASE_FIELDS, "signature": "synthetic-signature"})))
    fields[field] += "tampered"

    with pytest.raises(ContextEditorAuthError, match="authentication signature"):
        _validate(urlencode(fields))


def test_invalid_signature_is_rejected_before_decoding_user_data():
    """Unverified user JSON cannot become an identity or an authorization decision."""
    data = _signed_data(token="another-synthetic-token")
    with patch("bot.context_editor_auth.json.loads") as decode_user:
        with pytest.raises(ContextEditorAuthError, match="authentication signature") as failure:
            _validate(data)

    decode_user.assert_not_called()
    assert BOT_TOKEN not in str(failure.value)
    assert data not in str(failure.value)


@pytest.mark.parametrize(
    "data",
    [
        None,
        b"not-text",
        "",
        "not-a-query",
        pytest.param("x" * 16_385, id="oversized-launch"),
        pytest.param("&".join(f"key{number}=value" for number in range(33)), id="too-many-fields"),
        "user=%FF&hash=" + "0" * 64,
        "query_id=\ud800&hash=" + "0" * 64,
        _signed_data() + "&%75ser=another-user",
        _signed_data() + "&hash=" + "0" * 64,
        _signed_data().split("&hash=")[0],
        _signed_data().replace("hash=", "hash=z"),
        "hash=" + "é" * 64,
    ],
)
def test_malformed_or_ambiguous_launch_data_is_rejected(data):
    """Malformed input must produce a controlled rejection before context access."""
    with pytest.raises(ContextEditorAuthError):
        _validate(data)


@pytest.mark.parametrize(
    "extra_fields",
    [
        {"": "blank key"},
        {"query_id": "query\ninjected=value"},
        {"bad\rkey": "value"},
    ],
)
def test_signed_fields_cannot_make_the_field_list_ambiguous(extra_fields):
    """Even signed fields must have unambiguous keys and line boundaries."""
    with pytest.raises(ContextEditorAuthError, match="authentication data"):
        _validate(_signed_data({**BASE_FIELDS, **extra_fields}))


@pytest.mark.parametrize("auth_date", [None, "", "0", "-1", "1.5", "１２３", "+123", "123 "])
def test_invalid_or_missing_auth_date_is_rejected(auth_date):
    """A signed launch must carry a positive integer Unix timestamp."""
    fields = BASE_FIELDS.copy()
    if auth_date is None:
        fields.pop("auth_date")
    else:
        fields["auth_date"] = auth_date

    with pytest.raises(ContextEditorAuthError, match="authentication date"):
        _validate(_signed_data(fields))


@pytest.mark.parametrize(
    "user",
    [
        None,
        "not-json",
        "null",
        "[]",
        "{}",
        json.dumps({"id": str(USER_ID)}),
        json.dumps({"id": True}),
        json.dumps({"id": float(USER_ID)}),
    ],
)
def test_signed_launch_requires_a_user_with_an_integer_id(user):
    """Telegram user IDs cannot be inferred from strings, booleans, or other JSON."""
    fields = BASE_FIELDS.copy()
    if user is None:
        fields.pop("user")
    else:
        fields["user"] = user

    with pytest.raises(ContextEditorAuthError, match="user data"):
        _validate(_signed_data(fields))


def test_genuine_launch_from_another_user_is_rejected():
    """A valid Telegram signature does not grant another user access to private context."""
    data = _signed_data({**BASE_FIELDS, "user": json.dumps({"id": USER_ID + 1})})

    with pytest.raises(ContextEditorAuthError, match="not available"):
        _validate(data)


@pytest.mark.parametrize(
    "settings",
    [
        {"bot_token": ""},
        {"bot_token": None},
        {"allowed_user_id": 0},
        {"allowed_user_id": True},
        {"allowed_user_id": str(USER_ID)},
        {"max_age_seconds": 0},
        {"max_age_seconds": -1},
        {"max_age_seconds": True},
        {"now": 1.5},
        {"now": True},
    ],
)
def test_invalid_server_settings_are_distinct_from_rejected_launches(settings):
    """The HTTP layer can distinguish its own configuration error from a denied request."""
    with pytest.raises(ValueError) as failure:
        _validate(_signed_data(), **settings)

    assert not isinstance(failure.value, ContextEditorAuthError)
