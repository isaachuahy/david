import os
import sys
from dotenv import load_dotenv

# Add the project root to sys.path so we can import local modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from reasoning.pro_client import generate_sunday_review

def main():
    print("=== Testing Gemini Pro Client (Sunday Review) ===")
    load_dotenv()
    
    if not os.getenv("GEMINI_API_KEY"):
        print("Error: GEMINI_API_KEY is not set in your .env file.")
        return
        
    dummy_context = """<CONTEXT>
<GOALS>
Short-Term: Complete MVP for David.
</GOALS>
<WEEKLY_STATE>
[ ] Complete MVP for David.
</WEEKLY_STATE>
<DECISION_LOG>
Decided to use SQLite for persistence.
</DECISION_LOG>
<UPCOMING_CALENDAR>
No upcoming events scheduled.
</UPCOMING_CALENDAR>
</CONTEXT>"""
    
    dummy_past_events = """
- [2026-03-15T09:00:00Z] Deep Work: Architecture
- [2026-03-16T14:00:00Z] Gym / Workout
"""
    
    print("Sending dummy context to Gemini Pro...")
    print("Waiting for response (this may take a few seconds)...")
    
    try:
        response = generate_sunday_review(context_block=dummy_context, past_events_block=dummy_past_events)
        print("\n--- Response Received ---")
        print(f"MESSAGE:\n{response.message}\n")
        print(f"STATE CHANGE SUMMARY:\n{response.state_change_summary}\n")
        print(f"NEW WEEKLY STATE:\n{response.weekly_state_content}\n")
        print(f"PROPOSED EVENTS ({len(response.proposed_events)}):")
        for event in response.proposed_events:
            print(f" - [{event.start_time} to {event.end_time}] {event.summary}: {event.description}")
        print("-------------------------")
        
    except Exception as e:
        print(f"\nAn error occurred: {e}")

if __name__ == "__main__":
    main()
