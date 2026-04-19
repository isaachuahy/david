from persistence.database import get_db, init_db


def test_init_db_creates_expected_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("DAVID_DB_PATH", str(tmp_path / "assistant.db"))

    init_db()

    table_names = set(get_db().table_names())
    assert {"calendar_writes", "sessions", "decisions", "weekly_snapshots"} <= table_names


def test_init_db_calendar_writes_includes_multi_calendar_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("DAVID_DB_PATH", str(tmp_path / "assistant.db"))

    init_db()

    columns = {column.name for column in get_db()["calendar_writes"].columns}
    assert {
        "action_type",
        "calendar_id",
        "target_event_id",
        "target_event_calendar_id",
        "created_event_id",
        "created_event_calendar_id",
    } <= columns
