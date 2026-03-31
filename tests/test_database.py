from persistence.database import get_db, init_db


def test_init_db_creates_expected_tables(tmp_path, monkeypatch):
    monkeypatch.setattr("persistence.database.DB_PATH", str(tmp_path / "assistant.db"))

    init_db()

    table_names = set(get_db().table_names())
    assert {"calendar_writes", "sessions", "decisions", "weekly_snapshots"} <= table_names
