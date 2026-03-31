import os
import sys
import uuid
from datetime import datetime, timezone

# Add the project root to sys.path so we can import local modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from persistence.database import get_db, init_db
from persistence.models import CalendarWriteRecord, CalendarWriteStatus


def main():
    print("=== Testing Database Operations ===")

    # Ensure tables exist just in case
    init_db()
    db = get_db()

    # 1. Test Insert
    print("\n[1] Testing WRITE to calendar_writes...")
    test_id = f"test_{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()

    test_record = CalendarWriteRecord(
        id=test_id,
        summary="David - DB Write Test",
        start_time=now_iso,
        end_time=now_iso,
        description="Testing SQLite persistence.",
        status=CalendarWriteStatus.PENDING,
        created_at=now_iso,
    )

    db["calendar_writes"].insert(test_record.model_dump())
    print(f"Inserted record with ID: {test_id}")

    # 2. Test Read
    print("\n[2] Testing READ from calendar_writes...")
    row = db["calendar_writes"].get(test_id)
    print(row)
    print(f"Success! Read record: '{row['summary']}' (Status: {row['status']})")

    # 3. Test Delete (Clean up)
    print("\n[3] Cleaning up test record...")
    db["calendar_writes"].delete(test_id)
    print("Clean up complete.")


if __name__ == "__main__":
    main()
