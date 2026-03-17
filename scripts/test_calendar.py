from datetime import datetime, timezone, timedelta
from integrations.calendar import get_upcoming_events, insert_event

def main():
    print("=== Testing Google Calendar Integration ===")
    
    # 1. Test Read
    print("\n[1] Testing READ functionality...")
    events = get_upcoming_events(max_results=5)
    
    if not events:
        print("No upcoming events found or an error occurred.")
    else:
        print("--- UPCOMING EVENTS ---")
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'Busy / No Title')
            print(f"[{start}] {summary}")
            
    # 2. Test Write
    print("\n[2] Testing WRITE functionality...")
    print("Creating a 15-minute test event starting now...")
    
    now = datetime.now(timezone.utc)
    end = now + timedelta(minutes=15)
    
    created_event = insert_event(
        summary="David - API Write Test",
        start_time=now,
        end_time=end,
        description="Testing automated calendar writes from David."
    )
    
    if created_event:
        print(f"Success! Event created: {created_event.get('htmlLink')}")
    else:
        print("Failed to create event.")

if __name__ == "__main__":
    main()
