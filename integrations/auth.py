import os
from typing import Any
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# We need full access to read and write events
SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_calendar_credentials() -> Any:
    """
    Gets valid user credentials from storage or initiates the OAuth2 flow.
    Returns a Credentials object suitable for building Google API clients.
    """
    creds = None
    
    token_path = os.getenv('GOOGLE_TOKEN_PATH', 'token.json')
    credentials_path = os.getenv('GOOGLE_CREDENTIALS_PATH', 'credentials.json')

    # Load existing token if available
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            logger.warning(f"Failed to load existing token: {e}")

    # If there are no valid credentials, prompt user to log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Google Calendar credentials...")
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.error(f"Failed to refresh token: {e}. Re-authenticating...")
                creds = None

        if not creds:
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(
                    f"OAuth credentials not found at {credentials_path}. "
                    "Please download the OAuth 2.0 Client ID JSON from Google Cloud Console."
                )
            logger.info("Starting new Google OAuth flow. Please check your browser.")
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save the credentials for the next run so David can run headless
        with open(token_path, 'w') as token_file:
            token_file.write(creds.to_json())
            logger.success(f"Saved new Google Calendar token to {token_path}")

    return creds