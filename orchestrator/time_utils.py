from datetime import datetime

def parse_iso(iso_str: str) -> datetime:
    """
    Parses an ISO 8601 string into a timezone-aware datetime object.
    Safely handles the 'Z' suffix commonly output by LLMs and APIs.
    """
    clean_str = iso_str.replace('Z', '+00:00')
    return datetime.fromisoformat(clean_str)
